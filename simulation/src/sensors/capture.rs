//! Opt-in real GPU readback. Frozen delivery can only reuse completed RGB.
use super::model::{CaptureTicket, FrameInfo};
use crate::{
    domain::ShipCameraId,
    model::{SensorDelivery, SensorFaultMode, SensorUnitId, Snapshot},
};
use bevy::{prelude::*, render::gpu_readback::ReadbackComplete};
use std::{cell::RefCell, collections::VecDeque};
use wasm_bindgen::prelude::*;

pub(super) struct Request {
    pub camera: ShipCameraId,
    pub token: u32,
    pub trial_revision: u32,
    pub sensor_revision: u32,
}

struct Pending {
    camera: ShipCameraId,
    token: u32,
    resolve: js_sys::Function,
    reject: js_sys::Function,
}

struct CompletedCapture {
    info: FrameInfo,
    rgb: Vec<u8>,
    trial_revision: u32,
    sensor_revision: u32,
    pose_delivered: bool,
}

thread_local! {
    static QUEUE: RefCell<VecDeque<Request>> = const { RefCell::new(VecDeque::new()) };
    static PENDING: RefCell<Vec<Pending>> = const { RefCell::new(Vec::new()) };
    static NEXT: RefCell<u32> = const { RefCell::new(0) };
    static COMPLETED: RefCell<[Option<CompletedCapture>; 2]> = const { RefCell::new([None, None]) };
}

pub(super) fn unit(camera: ShipCameraId) -> SensorUnitId {
    match camera {
        ShipCameraId::Top => SensorUnitId::CameraTop,
        ShipCameraId::Bottom => SensorUnitId::CameraBottom,
    }
}

pub(super) fn mode(snapshot: &Snapshot, camera: ShipCameraId) -> SensorFaultMode {
    snapshot
        .sensor_state
        .as_ref()
        .map_or(SensorFaultMode::Nominal, |state| state.mode(unit(camera)))
}

pub(super) fn cached_info(camera: ShipCameraId) -> Option<FrameInfo> {
    let trial = crate::session::trial_revision();
    COMPLETED.with(|completed| {
        completed.borrow()[camera.index()]
            .as_ref()
            .filter(|capture| capture.trial_revision == trial)
            .map(|capture| capture.info.clone())
    })
}

pub(super) fn completed_revision(camera: ShipCameraId) -> Option<u32> {
    let trial = crate::session::trial_revision();
    COMPLETED.with(|completed| {
        completed.borrow()[camera.index()]
            .as_ref()
            .filter(|capture| capture.trial_revision == trial)
            .map(|capture| capture.sensor_revision)
    })
}

fn failure(camera: ShipCameraId, code: &str, message: &str) -> JsValue {
    js_sys::JSON::parse(
        &serde_json::json!({"code": code, "message": message, "camera": camera}).to_string(),
    )
    .expect("capture error contains valid JSON")
}

fn exported_frame(info: &FrameInfo, rgb: &[u8], pose_delivered: bool) -> JsValue {
    let mut metadata: serde_json::Value =
        serde_json::from_str(&serde_json::to_string(info).expect("finite sensor metadata"))
            .expect("valid frame JSON");
    if !pose_delivered {
        // The renderer's exact pose is local navigation readback, not vision.
        metadata["position_m"] = serde_json::Value::Null;
        metadata["rotation"] = serde_json::Value::Null;
    }
    let result = js_sys::JSON::parse(&metadata.to_string()).expect("sensor metadata is valid JSON");
    let pixels = js_sys::Uint8Array::from(rgb);
    let _ = js_sys::Reflect::set(&result, &JsValue::from_str("rgb"), &pixels);
    result
}

pub(crate) fn request_capture(camera: ShipCameraId) -> js_sys::Promise {
    let snapshot = crate::snapshot();
    match mode(&snapshot, camera) {
        SensorFaultMode::Unavailable => {
            return js_sys::Promise::reject(&failure(
                camera,
                "sensor_unavailable",
                "Ship camera delivery is unavailable.",
            ));
        }
        SensorFaultMode::Frozen => {
            return COMPLETED.with(|completed| {
                let completed = completed.borrow();
                match completed[camera.index()]
                    .as_ref()
                    .filter(|capture| capture.trial_revision == crate::session::trial_revision())
                {
                    Some(capture) => js_sys::Promise::resolve(&exported_frame(
                        &capture.info,
                        &capture.rgb,
                        capture.pose_delivered,
                    )),
                    None => js_sys::Promise::reject(&failure(
                        camera,
                        "sensor_no_sample",
                        "Frozen camera has no completed RGB capture in this trial.",
                    )),
                }
            });
        }
        SensorFaultMode::Nominal => {}
    }
    if !super::systems::configured() {
        return js_sys::Promise::reject(&failure(
            camera,
            "sensor_no_sample",
            "Ship cameras are not initialized yet.",
        ));
    }
    if PENDING.with(|pending| {
        pending
            .borrow()
            .iter()
            .any(|request| request.camera == camera)
    }) {
        return js_sys::Promise::reject(&failure(
            camera,
            "sensor_no_sample",
            "A capture for this ship camera is already pending.",
        ));
    }
    let token = NEXT.with(|next| {
        let mut next = next.borrow_mut();
        *next = next.wrapping_add(1);
        *next
    });
    let trial_revision = crate::session::trial_revision();
    let sensor_revision = crate::session::sensor_revision();
    js_sys::Promise::new(&mut |resolve, reject| {
        PENDING.with(|pending| {
            pending.borrow_mut().push(Pending {
                camera,
                token,
                resolve,
                reject,
            })
        });
        QUEUE.with(|queue| {
            queue.borrow_mut().push_back(Request {
                camera,
                token,
                trial_revision,
                sensor_revision,
            })
        });
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
        let _ = request.reject.call1(
            &JsValue::NULL,
            &failure(camera, "sensor_no_sample", message),
        );
    }
}

pub(crate) fn invalidate_pending() {
    QUEUE.with(|queue| queue.borrow_mut().clear());
    let pending = PENDING.with(|pending| std::mem::take(&mut *pending.borrow_mut()));
    for request in pending {
        let _ = request.reject.call1(
            &JsValue::NULL,
            &failure(
                request.camera,
                "sensor_no_sample",
                "Capture invalidated by a sensor policy or trial change.",
            ),
        );
    }
}

pub(crate) fn invalidate_trial() {
    invalidate_pending();
    COMPLETED.with(|completed| *completed.borrow_mut() = [None, None]);
}

pub(super) fn complete(
    event: On<ReadbackComplete>,
    tickets: Query<&CaptureTicket>,
    mut commands: Commands,
) {
    let Ok(ticket) = tickets.get(event.entity) else {
        return;
    };
    commands.entity(event.entity).despawn();
    let Some(request) = take(ticket.token, ticket.info.camera) else {
        return;
    };
    let camera = ticket.info.camera;
    let snapshot = crate::snapshot();
    let unavailable = mode(&snapshot, camera) == SensorFaultMode::Unavailable;
    if ticket.trial_revision != crate::session::trial_revision()
        || ticket.sensor_revision != crate::session::sensor_revision()
        || mode(&snapshot, camera) != SensorFaultMode::Nominal
    {
        let code = if unavailable {
            "sensor_unavailable"
        } else {
            "sensor_no_sample"
        };
        let _ = request.reject.call1(
            &JsValue::NULL,
            &failure(
                camera,
                code,
                "Sensor delivery changed before this capture completed.",
            ),
        );
        return;
    }
    let expected = ticket.info.width as usize * ticket.info.height as usize * 4;
    if event.data.len() != expected {
        let _ = request.reject.call1(
            &JsValue::NULL,
            &failure(
                camera,
                "sensor_no_sample",
                "Sensor readback has an unexpected row stride or format.",
            ),
        );
        return;
    }
    let rgb: Vec<u8> = event
        .data
        .as_chunks::<4>()
        .0
        .iter()
        .flat_map(|pixel| [pixel[0], pixel[1], pixel[2]])
        .collect();
    let pose_delivered = snapshot
        .sensor_state
        .as_ref()
        .is_none_or(|state| state.navigation_delivery() == SensorDelivery::Ready);
    let result = exported_frame(&ticket.info, &rgb, pose_delivered);
    COMPLETED.with(|completed| {
        completed.borrow_mut()[camera.index()] = Some(CompletedCapture {
            info: ticket.info.clone(),
            rgb,
            trial_revision: ticket.trial_revision,
            sensor_revision: ticket.sensor_revision,
            pose_delivered,
        })
    });
    let _ = request.resolve.call1(&JsValue::NULL, &result);
}
