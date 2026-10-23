//! WebAssembly facade. Domain state, effects, rendering and protocol parsing are
//! implemented by their respective modules rather than by this entry point.
mod api;
mod app;
pub mod astrophysics;
mod camera;
mod controls;
pub mod domain;
mod guidance;
mod hud;
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
pub mod terrain;

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
pub fn ship_camera_status() -> String {
    sensors::status()
}

/// Read-only pathway preview. This never dispatches a simulation command.
#[wasm_bindgen]
pub fn navigation_status() -> String {
    navigation::status()
}

#[wasm_bindgen]
pub fn ship_camera_capture(camera: &str) -> Result<js_sys::Promise, JsValue> {
    let id = serde_json::from_value(serde_json::Value::String(camera.to_owned()))
        .map_err(|error| JsValue::from_str(&format!("Unknown ship camera: {error}")))?;
    Ok(sensors::request_capture(id))
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
pub fn platform_state(fullscreen: bool, maximized: bool, message: &str) {
    session::set_viewport(fullscreen, maximized, message);
}
