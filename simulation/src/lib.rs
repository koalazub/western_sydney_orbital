use bevy::{
    prelude::*,
    render::{
        Render, RenderApp, RenderSystems,
        render_resource::{CachedPipelineState, PipelineCache},
    },
    window::WindowResolution,
};
use std::cell::RefCell;
use wasm_bindgen::prelude::*;

pub mod model;
mod physics;
mod scene;

thread_local! {
    static SIMULATION: RefCell<physics::Simulation> = RefCell::new(physics::Simulation::new(100));
    static RENDERER: RefCell<RendererState> = RefCell::new(RendererState::default());
}

#[derive(Default, serde::Serialize)]
struct RendererState {
    started: bool,
    ready: bool,
    rendered_frames: u32,
    compiled_pipelines: usize,
    pending_pipelines: usize,
    error: Option<String>,
}

pub fn snapshot() -> model::Snapshot {
    SIMULATION.with(|simulation| simulation.borrow().snapshot())
}

#[wasm_bindgen]
pub fn command(json: &str) -> String {
    let result = (|| {
        if json.len() > 1024 * 1024 {
            return Err("Command exceeds the 1 MiB limit".to_string());
        }
        let request: serde_json::Value = serde_json::from_str(json).map_err(|e| e.to_string())?;
        if request["op"] == "view" {
            scene::view_command(&request)?;
            return Ok(snapshot());
        }
        SIMULATION.with(|simulation| simulation.borrow_mut().execute(request))
    })();
    match result {
        Ok(state) => serde_json::json!({"ok": true, "state": state}).to_string(),
        Err(error) => serde_json::json!({"ok": false, "error": error}).to_string(),
    }
}

/// Strategy input excludes the viewer's debris truth state.
#[wasm_bindgen]
pub fn observation() -> String {
    SIMULATION.with(|simulation| simulation.borrow().observation().to_string())
}

#[wasm_bindgen]
pub fn renderer_status() -> String {
    RENDERER.with(|state| serde_json::to_string(&*state.borrow()).unwrap())
}

fn renderer_error(message: String) {
    RENDERER.with(|state| {
        let mut state = state.borrow_mut();
        state.ready = false;
        state.error = Some(message.clone());
    });
    #[cfg(target_arch = "wasm32")]
    if let Ok(callback) = js_sys::Reflect::get(&js_sys::global(), &"__wsorbEngineError".into()) {
        if let Some(callback) = callback.dyn_ref::<js_sys::Function>() {
            let _ = callback.call1(&JsValue::NULL, &message.into());
        }
    }
}

/// Runs after the render graph, not when App::run merely schedules startup.
fn observe_renderer(cache: Res<PipelineCache>, mut stable_frames: Local<u32>) {
    let mut compiled = 0;
    for pipeline in cache.pipelines() {
        match &pipeline.state {
            CachedPipelineState::Ok(_) => compiled += 1,
            CachedPipelineState::Err(error) => {
                renderer_error(format!("Graphics pipeline failed: {error}"));
                return;
            }
            _ => {}
        }
    }
    let pending = cache.waiting_pipelines().count();
    if compiled >= 2 && pending == 0 {
        *stable_frames = stable_frames.saturating_add(1);
    } else {
        *stable_frames = 0;
    }
    RENDERER.with(|state| {
        let mut state = state.borrow_mut();
        state.compiled_pipelines = compiled;
        state.pending_pipelines = pending;
        state.rendered_frames = state.rendered_frames.saturating_add(1);
        if state.error.is_none() && *stable_frames >= 3 {
            state.ready = true;
        }
    });
}

#[wasm_bindgen]
pub fn start_simulation(canvas_selector: &str) -> Result<(), JsValue> {
    let already_started = RENDERER.with(|state| {
        let mut state = state.borrow_mut();
        let started = state.started;
        state.started = true;
        started
    });
    if already_started {
        return Err(JsValue::from_str(
            "Renderer already started. Reload the page to retry.",
        ));
    }
    let mut app = App::new();
    app.insert_resource(ClearColor(Color::srgb(0.035, 0.045, 0.06)))
        .add_plugins(DefaultPlugins.set(WindowPlugin {
            primary_window: Some(Window {
                canvas: Some(canvas_selector.to_string()),
                fit_canvas_to_parent: true,
                prevent_default_event_handling: false,
                resolution: WindowResolution::new(1000, 700),
                ..default()
            }),
            ..default()
        }))
        .add_systems(Startup, scene::setup)
        .add_systems(Update, scene::sync_scene);
    app.sub_app_mut(RenderApp)
        .add_systems(Render, observe_renderer.in_set(RenderSystems::Cleanup));
    // LogPlugin installs a hook too. Install our browser failure signal last.
    std::panic::set_hook(Box::new(|info| {
        renderer_error(format!("Bevy stopped: {info}"))
    }));
    app.run();
    Ok(())
}
