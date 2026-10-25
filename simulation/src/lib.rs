//! WebAssembly facade. Domain state, effects, rendering and protocol parsing are
//! implemented by their respective modules rather than by this entry point.
mod api;
mod app;
pub mod astrophysics;
mod camera;
mod controls;
pub mod domain;
mod hud;
mod launchpad;
mod mission;
pub mod model;
mod navigation;
mod physics;
mod propulsion;
mod runtime;
pub mod scenario;
mod scene;
mod sensors;
mod session;
mod telemetry;
pub mod terrain;
mod thrusters;

pub(crate) use app::EngineSet;
pub(crate) use runtime::PlaybackState;
pub(crate) use session::{request_action, snapshot};

use wasm_bindgen::prelude::*;

#[wasm_bindgen]
pub fn start_simulation(canvas_selector: &str) -> Result<(), JsValue> {
    app::start(canvas_selector)
}

#[wasm_bindgen]
pub fn command(json: &str) -> String {
    api::execute(json)
}

/// Real control acknowledgement without copying the full debug world into JSON.
#[wasm_bindgen]
pub fn control(json: &str) -> String {
    api::control(json)
}

/// Local debug physics measurements, not SDK radar or a flight-policy input.
#[wasm_bindgen]
pub fn field_diagnostics() -> String {
    match session::debris_dynamics() {
        Ok(report) => serde_json::json!({"ok": true, "report": report}).to_string(),
        Err(error) => serde_json::json!({"ok": false, "error": error}).to_string(),
    }
}

#[wasm_bindgen]
pub fn observation() -> String {
    session::observation().to_string()
}

#[wasm_bindgen]
pub fn renderer_status() -> String {
    app::status()
}

#[wasm_bindgen]
pub fn camera_status() -> String {
    serde_json::to_string(&camera::status()).expect("camera reports contain finite values")
}

#[wasm_bindgen]
pub fn hud_status() -> String {
    hud::status()
}

#[wasm_bindgen]
pub fn ship_camera_status() -> String {
    sensors::status()
}

#[wasm_bindgen]
pub fn pan_tilt_camera(camera: &str, yaw_deg: f64, pitch_deg: f64) -> String {
    let result = camera_id(camera).and_then(|id| sensors::pan_tilt(id, yaw_deg, pitch_deg));
    match result {
        Ok(status) => serde_json::json!({
            "ok": true,
            "status": serde_json::from_str::<serde_json::Value>(&status)
                .expect("sensor status is valid JSON"),
        })
        .to_string(),
        Err(error) => serde_json::json!({"ok": false, "error": error}).to_string(),
    }
}

/// Read-only pathway preview. This never dispatches a simulation command.
#[wasm_bindgen]
pub fn navigation_status() -> String {
    navigation::status()
}

/// Draw Python-computed waypoints. This never plans a route or commands thrust.
#[wasm_bindgen]
pub fn set_navigation_preview(json: &str) -> String {
    match navigation::submit(json) {
        Ok(()) => serde_json::json!({"ok": true}).to_string(),
        Err(api::CommandError::StaleContext) => serde_json::json!({
            "ok": false, "code": "stale_context", "error": api::CommandError::StaleContext.to_string(),
        }).to_string(),
        Err(api::CommandError::Rejected(error)) =>
            serde_json::json!({"ok": false, "error": error}).to_string(),
    }
}

/// Physical practice-world query, drawn only in debug views. Not an SDK sensor.
#[wasm_bindgen]
pub fn lookahead_raycast(json: &str) -> String {
    let result = (|| {
        if json.len() > 4096 {
            return Err("Look-ahead query exceeds the 4 KiB limit".to_owned());
        }
        let query: model::LookaheadQuery =
            serde_json::from_str(json).map_err(|error| error.to_string())?;
        session::raycast(&query)
    })();
    match result {
        Ok(report) => {
            let response = serde_json::json!({"ok": true, "report": &report}).to_string();
            navigation::publish_raycast(report);
            response
        }
        Err(error) => serde_json::json!({"ok": false, "error": error}).to_string(),
    }
}

/// Local practice telemetry with explicit units/frame, not an STP SDK endpoint.
#[wasm_bindgen]
pub fn telemetry_status() -> String {
    telemetry::status()
}

#[wasm_bindgen]
pub fn ship_camera_capture(camera: &str) -> Result<js_sys::Promise, JsValue> {
    let id = camera_id(camera).map_err(|error| JsValue::from_str(&error))?;
    Ok(sensors::request_capture(id))
}

fn camera_id(camera: &str) -> Result<domain::ShipCameraId, String> {
    serde_json::from_value(serde_json::Value::String(camera.to_owned()))
        .map_err(|error| format!("Unknown ship camera: {error}"))
}

#[wasm_bindgen]
pub fn runtime_status() -> String {
    session::runtime_report()
}

#[wasm_bindgen]
pub fn set_viewport_input(active: bool, locked: bool) {
    camera::set_input_active(active, locked);
}

/// Physical movement keys only. Routing and ship commands remain in Rust.
#[wasm_bindgen]
pub fn set_movement_keys(mask: u16) {
    controls::set_movement_keys(mask);
}

#[wasm_bindgen]
pub fn key_bindings_status() -> String {
    controls::bindings_status()
}

#[wasm_bindgen]
pub fn set_key_bindings(json: &str) -> String {
    match controls::set_bindings(json) {
        Ok(()) => serde_json::json!({"ok": true}).to_string(),
        Err(error) => serde_json::json!({"ok": false, "error": error}).to_string(),
    }
}

#[wasm_bindgen]
pub fn pinch_zoom(log_distance_delta: f32) {
    camera::pinch_zoom(log_distance_delta);
}

#[wasm_bindgen]
pub fn fullscreen_button_at(x: f32, y: f32) -> bool {
    hud::fullscreen_button_at(x, y)
}

#[wasm_bindgen]
pub fn fullscreen_button_focused() -> bool {
    hud::fullscreen_button_focused()
}

#[wasm_bindgen]
pub fn notebook_button_at(x: f32, y: f32) -> bool {
    hud::notebook_button_at(x, y)
}

#[wasm_bindgen]
pub fn notebook_button_focused() -> bool {
    hud::notebook_button_focused()
}

#[wasm_bindgen]
pub fn platform_state(fullscreen: bool, maximized: bool, message: &str) {
    session::set_viewport(fullscreen, maximized, message);
}
