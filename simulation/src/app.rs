use bevy::{
    prelude::*,
    render::{
        Render, RenderApp, RenderSystems,
        render_resource::{CachedPipelineState, PipelineCache},
    },
    shader::ShaderCacheError,
};
use std::cell::RefCell;
use wasm_bindgen::prelude::*;

#[derive(SystemSet, Debug, Clone, PartialEq, Eq, Hash)]
pub(crate) enum EngineSet {
    Input,
    Runtime,
    Camera,
    Scene,
    Hud,
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

thread_local! { static RENDERER: RefCell<RendererState> = RefCell::new(RendererState::default()); }

pub fn status() -> String {
    RENDERER.with(|state| {
        serde_json::to_string(&*state.borrow()).expect("renderer status is serializable")
    })
}

fn fail(message: String) {
    RENDERER.with(|state| {
        let mut state = state.borrow_mut();
        state.ready = false;
        state.error = Some(message.clone());
    });
    #[cfg(target_arch = "wasm32")]
    if let Ok(callback) = js_sys::Reflect::get(&js_sys::global(), &"__wsorbEngineError".into())
        && let Some(callback) = callback.dyn_ref::<js_sys::Function>()
    {
        let _ = callback.call1(&JsValue::NULL, &message.into());
    }
}

fn observe_frame(cache: Res<PipelineCache>, mut stable_frames: Local<u32>) {
    let mut compiled = 0;
    for pipeline in cache.pipelines() {
        match &pipeline.state {
            CachedPipelineState::Ok(_) => compiled += 1,
            // Bevy requeues these while embedded shader assets finish loading.
            CachedPipelineState::Err(
                ShaderCacheError::ShaderNotLoaded(_)
                | ShaderCacheError::ShaderImportNotYetAvailable,
            ) => {}
            CachedPipelineState::Err(error) => {
                fail(format!("Graphics pipeline failed: {error}"));
                return;
            }
            CachedPipelineState::Queued | CachedPipelineState::Creating(_) => {}
        }
    }
    let pending = cache.waiting_pipelines().count();
    *stable_frames = if compiled >= 2 && pending == 0 {
        stable_frames.saturating_add(1)
    } else {
        0
    };
    RENDERER.with(|state| {
        let mut state = state.borrow_mut();
        state.compiled_pipelines = compiled;
        state.pending_pipelines = pending;
        state.rendered_frames = state.rendered_frames.saturating_add(1);
        state.ready = state.error.is_none() && *stable_frames >= 3;
    });
}

pub fn start(canvas_selector: &str) -> Result<(), JsValue> {
    let already_started =
        RENDERER.with(|state| std::mem::replace(&mut state.borrow_mut().started, true));
    if already_started {
        return Err(JsValue::from_str(
            "Renderer already started. Reload to retry.",
        ));
    }
    let mut app = App::new();
    app.insert_resource(ClearColor(Color::BLACK))
        .init_resource::<crate::PlaybackState>()
        .add_plugins(DefaultPlugins.set(WindowPlugin {
            primary_window: Some(Window {
                canvas: Some(canvas_selector.into()),
                fit_canvas_to_parent: true,
                prevent_default_event_handling: false,
                ..default()
            }),
            ..default()
        }))
        .configure_sets(
            Update,
            (
                EngineSet::Input,
                EngineSet::Runtime,
                EngineSet::Camera,
                EngineSet::Scene,
                EngineSet::Hud,
            )
                .chain(),
        )
        .add_systems(Startup, crate::scene::setup)
        .add_systems(
            Update,
            crate::session::advance_world.in_set(EngineSet::Runtime),
        )
        .add_systems(Update, crate::scene::sync_scene.in_set(EngineSet::Scene));
    crate::camera::install(&mut app);
    crate::hud::install(&mut app);
    app.sub_app_mut(RenderApp)
        .add_systems(Render, observe_frame.in_set(RenderSystems::Cleanup));
    // Bevy's logging plugin also installs a hook; the failure bridge goes last.
    std::panic::set_hook(Box::new(|info| fail(format!("Bevy stopped: {info}"))));
    app.run();
    Ok(())
}
