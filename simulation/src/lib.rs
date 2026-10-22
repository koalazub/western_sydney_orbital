//! WebAssembly facade. Domain state, effects, rendering and protocol parsing are
//! implemented by their respective modules rather than by this entry point.
mod api;
mod app;
pub mod astrophysics;
mod camera;
pub mod domain;
mod guidance;
mod hud;
mod mission;
pub mod model;
mod physics;
mod runtime;
pub mod scenario;
mod scene;
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
pub fn runtime_status() -> String {
    session::runtime_report()
}

#[wasm_bindgen]
pub fn set_viewport_input(active: bool, locked: bool) {
    camera::set_input_active(active, locked);
}

#[wasm_bindgen]
pub fn platform_state(fullscreen: bool, maximized: bool, message: &str) {
    session::set_viewport(fullscreen, maximized, message);
}
