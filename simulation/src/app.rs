use bevy::{
    camera::NormalizedRenderTarget,
    core_pipeline::core_3d::Opaque3d,
    ecs::{query::ROQueryItem, system::SystemParamItem},
    pbr::DrawMaterial,
    prelude::*,
    render::{
        Extract, ExtractSchedule, Render, RenderApp, RenderStartup, RenderSystems,
        camera::ExtractedCamera,
        error_handler::{RenderErrorHandler, RenderErrorPolicy},
        render_phase::{
            DrawFunctions, PhaseItem, PhaseItemExtraIndex, RenderCommand, RenderCommandResult,
            RenderCommandState, TrackedRenderPass,
        },
        render_resource::{CachedPipelineState, PipelineCache, PipelineDescriptor},
        renderer::{RenderAdapterInfo, RenderQueue},
        sync_world::{MainEntity, RenderEntity},
        view::{ExtractedWindow, ViewTarget},
    },
    shader::ShaderCacheError,
    window::PrimaryWindow,
};
use std::{
    cell::RefCell,
    sync::{
        Arc,
        atomic::{AtomicBool, AtomicU32, Ordering},
    },
};
use wasm_bindgen::prelude::*;

#[derive(SystemSet, Debug, Clone, PartialEq, Eq, Hash)]
pub(crate) enum EngineSet {
    Input,
    Runtime,
    Camera,
    Scene,
    Hud,
}

#[derive(Clone, Copy, Default, PartialEq, Eq, serde::Serialize)]
#[serde(rename_all = "snake_case")]
enum StartupStage {
    #[default]
    Initializing,
    PreparingView,
    DrawingWorld,
    Presenting,
    WaitingForGpu,
    Ready,
    Failed,
}

#[derive(serde::Serialize)]
struct AdapterState {
    name: String,
    backend: String,
    device_type: String,
    driver: String,
}

#[derive(Default, serde::Serialize)]
struct RendererState {
    started: bool,
    ready: bool,
    stage: StartupStage,
    rendered_frames: u32,
    submitted_primary_frames: u32,
    completed_primary_frame: bool,
    primary_world_draws: u32,
    primary_viewport: Option<[u32; 2]>,
    primary_target_size: Option<[u32; 2]>,
    adapter: Option<AdapterState>,
    compiled_pipelines: usize,
    pending_pipelines: usize,
    pending_pipeline_labels: Vec<String>,
    error: Option<String>,
    #[serde(skip)]
    completion: Arc<AtomicBool>,
}

impl RendererState {
    fn refresh_ready(&mut self) {
        self.completed_primary_frame = self.completion.load(Ordering::Acquire);
        if self.error.is_some() {
            self.ready = false;
            self.stage = StartupStage::Failed;
        } else if self.submitted_primary_frames > 0 && self.completed_primary_frame {
            // Startup is a latch. Later sensor views, resizes and shader variants
            // may queue more pipelines without invalidating a working renderer.
            self.ready = true;
            self.stage = StartupStage::Ready;
        }
    }
}

thread_local! { static RENDERER: RefCell<RendererState> = RefCell::new(RendererState::default()); }

pub fn status() -> String {
    RENDERER.with(|state| {
        let mut state = state.borrow_mut();
        state.refresh_ready();
        serde_json::to_string(&*state).expect("renderer status is serializable")
    })
}

fn fail(message: String) {
    RENDERER.with(|state| {
        let mut state = state.borrow_mut();
        state.ready = false;
        state.stage = StartupStage::Failed;
        state.error = Some(message.clone());
    });
    #[cfg(target_arch = "wasm32")]
    if let Ok(callback) = js_sys::Reflect::get(&js_sys::global(), &"__wsorbEngineError".into())
        && let Some(callback) = callback.dyn_ref::<js_sys::Function>()
    {
        let _ = callback.call1(&JsValue::NULL, &message.into());
    }
}

/// Replaced on every extraction, so a successful draw belongs to this frame.
#[derive(Component, Default)]
struct PrimaryGameFrame {
    world_draws: AtomicU32,
}

fn extract_primary_view(
    mut commands: Commands,
    cameras: Extract<Query<RenderEntity, With<crate::camera::SimulationCamera>>>,
) {
    for camera in &cameras {
        commands.entity(camera).insert(PrimaryGameFrame::default());
    }
}

struct RecordWorldDraw;

impl<P: PhaseItem> RenderCommand<P> for RecordWorldDraw {
    type Param = ();
    type ViewQuery = Option<&'static PrimaryGameFrame>;
    type ItemQuery = ();

    fn render<'w>(
        item: &P,
        frame: ROQueryItem<'w, '_, Self::ViewQuery>,
        _entity: Option<ROQueryItem<'w, '_, Self::ItemQuery>>,
        _param: SystemParamItem<'w, '_, Self::Param>,
        _pass: &mut TrackedRenderPass<'w>,
    ) -> RenderCommandResult {
        // This command runs only after DrawMaterial succeeds, including its
        // pipeline, bind groups, GPU preprocessing and mesh draw. A clear or
        // UI-only frame cannot satisfy it. Sensor image cameras have no marker.
        let has_instances = match item.extra_index() {
            PhaseItemExtraIndex::None | PhaseItemExtraIndex::DynamicOffset(_) => {
                !item.batch_range().is_empty()
            }
            PhaseItemExtraIndex::IndirectParametersIndex { range, .. } => !range.is_empty(),
        };
        if let Some(frame) = frame
            && has_instances
        {
            frame.world_draws.fetch_add(1, Ordering::Relaxed);
        }
        RenderCommandResult::Success
    }
}

fn pipeline_label(descriptor: &PipelineDescriptor) -> &str {
    match descriptor {
        PipelineDescriptor::RenderPipelineDescriptor(descriptor) => descriptor.label.as_deref(),
        PipelineDescriptor::ComputePipelineDescriptor(descriptor) => descriptor.label.as_deref(),
        PipelineDescriptor::MeshPipelineDescriptor(descriptor) => descriptor.label.as_deref(),
    }
    .unwrap_or("unnamed pipeline")
}

fn observe_device(adapter: Res<RenderAdapterInfo>) {
    RENDERER.with(|state| {
        let mut state = state.borrow_mut();
        state.adapter = Some(AdapterState {
            name: adapter.name.clone(),
            backend: format!("{:?}", adapter.backend),
            device_type: format!("{:?}", adapter.device_type),
            driver: adapter.driver.clone(),
        });
        if !state.ready && state.error.is_none() {
            state.stage = StartupStage::PreparingView;
        }
    });
}

fn observe_frame(
    cache: Res<PipelineCache>,
    queue: Res<RenderQueue>,
    cameras: Query<(&ExtractedCamera, Option<&ViewTarget>, &PrimaryGameFrame)>,
    windows: Query<(MainEntity, &ExtractedWindow), With<PrimaryWindow>>,
) {
    let mut compiled = 0;
    let mut pending_labels = Vec::new();
    for pipeline in cache.pipelines() {
        let waiting = match &pipeline.state {
            CachedPipelineState::Ok(_) => {
                compiled += 1;
                None
            }
            // Bevy requeues these while embedded shader assets finish loading.
            CachedPipelineState::Err(ShaderCacheError::ShaderNotLoaded(_)) => {
                Some("loading shader")
            }
            CachedPipelineState::Err(ShaderCacheError::ShaderImportNotYetAvailable) => {
                Some("loading shader import")
            }
            CachedPipelineState::Err(
                error @ (ShaderCacheError::ProcessShaderError(_)
                | ShaderCacheError::CreateShaderModule(_)),
            ) => {
                fail(format!(
                    "Graphics pipeline {} failed: {error}",
                    pipeline_label(&pipeline.descriptor)
                ));
                return;
            }
            CachedPipelineState::Queued => Some("queued"),
            CachedPipelineState::Creating(_) => Some("compiling"),
        };
        if let Some(waiting) = waiting
            && pending_labels.len() < 8
        {
            let label = format!("{}: {waiting}", pipeline_label(&pipeline.descriptor));
            if !pending_labels.contains(&label) {
                pending_labels.push(label);
            }
        }
    }
    let pending = cache.waiting_pipelines().count();
    let mut viewport = None;
    let mut target_size = None;
    let mut world_draws = 0;
    let mut presented = false;
    let mut stage = StartupStage::PreparingView;
    if let Ok((camera, target, frame)) = cameras.single() {
        viewport = camera.physical_viewport_size.map(|size| size.to_array());
        target_size = camera.physical_target_size.map(|size| size.to_array());
        world_draws = frame.world_draws.load(Ordering::Relaxed);
        stage = if target.is_none() || viewport.is_none() {
            StartupStage::PreparingView
        } else if world_draws == 0 {
            StartupStage::DrawingWorld
        } else {
            StartupStage::Presenting
        };
        if let Some(NormalizedRenderTarget::Window(window_ref)) = &camera.target
            && let Ok((window_entity, window)) = windows.single()
            && window_ref.entity() == window_entity
            && viewport.is_some_and(|size| size[0] > 0 && size[1] > 0)
            && target.is_some_and(ViewTarget::needs_present)
            && window.swap_chain_texture_view.is_some()
            && window.swap_chain_texture.is_none()
        {
            // Cleanup runs after render_system submits the render graph and
            // presents the used window texture. Its draw marker is frame-local.
            presented = world_draws > 0;
        }
    }
    RENDERER.with(|state| {
        let mut state = state.borrow_mut();
        state.compiled_pipelines = compiled;
        state.pending_pipelines = pending;
        state.pending_pipeline_labels = pending_labels;
        state.rendered_frames = state.rendered_frames.saturating_add(1);
        state.primary_world_draws = world_draws;
        state.primary_viewport = viewport;
        state.primary_target_size = target_size;
        if presented {
            if state.submitted_primary_frames == 0 {
                let completion = Arc::clone(&state.completion);
                queue.on_submitted_work_done(move || completion.store(true, Ordering::Release));
            }
            state.submitted_primary_frames = state.submitted_primary_frames.saturating_add(1);
        }
        if !state.ready && state.error.is_none() {
            state.stage = if state.submitted_primary_frames > 0 {
                StartupStage::WaitingForGpu
            } else {
                stage
            };
        }
        state.refresh_ready();
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
        .insert_resource(RenderErrorHandler(|error, main_world, _| {
            fail(format!("WebGPU {:?}: {}", error.ty, error.description));
            main_world.write_message(AppExit::error());
            RenderErrorPolicy::StopRendering
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
    crate::sensors::install(&mut app);
    crate::hud::install(&mut app);
    let render_app = app.sub_app_mut(RenderApp);
    // Keep Bevy's material draw unchanged, then observe only successful opaque
    // world draws. Rebind its public draw-function key before materials queue.
    let draw = RenderCommandState::<Opaque3d, (DrawMaterial, RecordWorldDraw)>::new(
        render_app.world_mut(),
    );
    render_app
        .world()
        .resource::<DrawFunctions<Opaque3d>>()
        .write()
        .add_with::<DrawMaterial, _>(draw);
    render_app
        .add_systems(RenderStartup, observe_device)
        .add_systems(ExtractSchedule, extract_primary_view)
        .add_systems(Render, observe_frame.in_set(RenderSystems::Cleanup));
    // Bevy's logging plugin also installs a hook; the failure bridge goes last.
    std::panic::set_hook(Box::new(|info| fail(format!("Bevy stopped: {info}"))));
    app.run();
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn readiness_requires_gpu_completion_not_pipeline_or_frame_counts() {
        let mut state = RendererState {
            rendered_frames: 100_000,
            compiled_pipelines: 100,
            submitted_primary_frames: 1,
            ..default()
        };
        state.refresh_ready();
        assert!(!state.ready);
        state.completion.store(true, Ordering::Release);
        state.refresh_ready();
        assert!(state.ready);
    }

    #[test]
    fn pending_sensor_or_background_pipelines_do_not_revoke_ready() {
        let mut state = RendererState {
            submitted_primary_frames: 1,
            ..default()
        };
        state.completion.store(true, Ordering::Release);
        state.refresh_ready();
        state.pending_pipelines = 50;
        state.refresh_ready();
        assert!(state.ready);
        assert!(state.stage == StartupStage::Ready);
    }

    #[test]
    fn completed_work_does_not_override_a_fatal_error() {
        let mut state = RendererState {
            submitted_primary_frames: 1,
            ..default()
        };
        state.completion.store(true, Ordering::Release);
        state.refresh_ready();
        assert!(state.ready);
        state.error = Some("device lost".into());
        state.refresh_ready();
        assert!(!state.ready);
        assert!(state.stage == StartupStage::Failed);
    }

    #[test]
    fn completion_without_a_primary_world_submission_is_not_ready() {
        let mut state = RendererState::default();
        state.completion.store(true, Ordering::Release);
        state.refresh_ready();
        assert!(!state.ready);
    }
}
