//! Opt-in GPU readback. Normal viewing never copies sensor pixels to the CPU.
use super::model::CaptureTicket;
use crate::domain::ShipCameraId;
use bevy::{prelude::*, render::gpu_readback::ReadbackComplete};
use std::{cell::RefCell, collections::VecDeque};
use wasm_bindgen::prelude::*;

pub(super) struct Request {
    pub camera: ShipCameraId,
    pub token: u32,
}

struct Pending {
    camera: ShipCameraId,
    token: u32,
    resolve: js_sys::Function,
    reject: js_sys::Function,
}

thread_local! {
    static QUEUE: RefCell<VecDeque<Request>> = const { RefCell::new(VecDeque::new()) };
    static PENDING: RefCell<Vec<Pending>> = const { RefCell::new(Vec::new()) };
    static NEXT: RefCell<u32> = const { RefCell::new(0) };
}

pub(crate) fn request_capture(camera: ShipCameraId) -> js_sys::Promise {
    if !super::systems::configured() {
        return js_sys::Promise::reject(&JsValue::from_str("Ship cameras are not initialized yet"));
    }
    if PENDING.with(|pending| {
        pending
            .borrow()
            .iter()
            .any(|request| request.camera == camera)
    }) {
        return js_sys::Promise::reject(&JsValue::from_str(
            "A capture for this ship camera is already pending",
        ));
    }
    let token = NEXT.with(|next| {
        let mut next = next.borrow_mut();
        *next = next.wrapping_add(1);
        *next
    });
    js_sys::Promise::new(&mut |resolve, reject| {
        PENDING.with(|pending| {
            pending.borrow_mut().push(Pending {
                camera,
                token,
                resolve,
                reject,
            })
        });
        QUEUE.with(|queue| queue.borrow_mut().push_back(Request { camera, token }));
    })
}

pub(super) fn drain() -> Vec<Request> {
    QUEUE.with(|queue| queue.borrow_mut().drain(..).collect())
}

fn take(token: u32, camera: ShipCameraId) -> Option<Pending> {
    PENDING.with(|pending| {
        let mut pending = pending.borrow_mut();
        let index = pending
            .iter()
            .position(|request| request.token == token && request.camera == camera)?;
        Some(pending.swap_remove(index))
    })
}

pub(super) fn reject(token: u32, camera: ShipCameraId, message: &str) {
    if let Some(request) = take(token, camera) {
        let _ = request
            .reject
            .call1(&JsValue::NULL, &JsValue::from_str(message));
    }
}

pub(super) fn complete(
    event: On<ReadbackComplete>,
    tickets: Query<&CaptureTicket>,
    mut commands: Commands,
) {
    let Ok(ticket) = tickets.get(event.entity) else {
        return;
    };
    let Some(request) = take(ticket.token, ticket.info.camera) else {
        return;
    };
    let expected = ticket.info.width as usize * ticket.info.height as usize * 4;
    if event.data.len() != expected {
        let _ = request.reject.call1(
            &JsValue::NULL,
            &JsValue::from_str("Sensor readback has an unexpected row stride or format"),
        );
    } else {
        let rgb: Vec<u8> = event
            .data
            .as_chunks::<4>()
            .0
            .iter()
            .flat_map(|pixel| [pixel[0], pixel[1], pixel[2]])
            .collect();
        let metadata =
            serde_json::to_string(&ticket.info).expect("sensor metadata is serializable");
        let result =
            js_sys::JSON::parse(&metadata).expect("serialized sensor metadata is valid JSON");
        let pixels = js_sys::Uint8Array::from(rgb.as_slice());
        let _ = js_sys::Reflect::set(&result, &JsValue::from_str("rgb"), &pixels);
        let _ = request.resolve.call1(&JsValue::NULL, &result);
    }
    commands.entity(event.entity).despawn();
}
