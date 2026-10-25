use bevy::{
    asset::AssetEventSystems,
    camera::NormalizedRenderTarget,
    core_pipeline::core_3d::Opaque3d,
    pbr::DrawMaterial,
    prelude::*,
    render::{
        ExtractSchedule, Render, RenderApp, RenderPlugin, RenderStartup, RenderSystems,
        camera::ExtractedCamera,
        error_handler::{RenderErrorHandler, RenderErrorPolicy},
        render_asset::RenderAssets,
        render_phase::{DrawFunctions, RenderCommandState},
        render_resource::{CachedPipelineState, PipelineCache, PipelineDescriptor},
        renderer::{RenderAdapterInfo, RenderDevice, RenderQueue},
        settings::{WgpuFeatures, WgpuSettings, WgpuSettingsPriority},
        sync_world::MainEntity,
        texture::GpuImage,
        view::{ExtractedWindow, ViewTarget},
    },
    shader::{ShaderCacheError, Source},
    window::PrimaryWindow,
};
use std::{
    borrow::Cow,
    cell::RefCell,
    ops::Range,
    sync::{
        Arc,
        atomic::{AtomicBool, Ordering},
    },
};
use wasm_bindgen::prelude::*;

mod browser_surface;
mod readiness;
use browser_surface::StartupError;
use readiness::{PrimaryGameFrame, RecordWorldDraw, extract_primary_view};

#[derive(SystemSet, Debug, Clone, PartialEq, Eq, Hash)]
pub(crate) enum EngineSet {
    Input,
    Runtime,
    Reports,
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
    features: String,
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
    primary_planetary_draw_mask: u32,
    primary_viewport: Option<[u32; 2]>,
    primary_target_size: Option<[u32; 2]>,
    adapter: Option<AdapterState>,
    compiled_pipelines: usize,
    pending_pipelines: usize,
    pending_pipeline_labels: Vec<String>,
    surface_maps_ready: bool,
    surface_maps_gpu_ready: bool,
    error: Option<String>,
    #[serde(skip)]
    completion: Arc<AtomicBool>,
}

impl RendererState {
    fn record_failure(&mut self, message: String) -> bool {
        if self.error.is_some() {
            return false;
        }
        self.ready = false;
        self.stage = StartupStage::Failed;
        self.error = Some(message);
        true
    }

    fn refresh_ready(&mut self) {
        self.completed_primary_frame = self.completion.load(Ordering::Acquire);
        if self.error.is_some() {
            self.ready = false;
            self.stage = StartupStage::Failed;
        } else if self.ready
            || (self.submitted_primary_frames > 0
                && self.completed_primary_frame
                && self.surface_maps_ready
                && self.surface_maps_gpu_ready
                && self.primary_planetary_draw_mask
                    == crate::scene::PlanetarySurface::ALL_DRAW_BITS
                && self.pending_pipelines == 0
                && self.pending_pipeline_labels.is_empty())
        {
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
    // A rejected shader can cause several later "module is invalid" errors.
    // Keep the first compiler/device error instead of replacing it with fallout.
    // A panic may occur while RENDERER is already borrowed. Still notify the
    // browser without triggering a second panic inside the panic hook.
    let first_failure = RENDERER
        .try_with(|state| {
            state
                .try_borrow_mut()
                .map_or(true, |mut state| state.record_failure(message.clone()))
        })
        .unwrap_or(true);
    if !first_failure {
        return;
    }
    #[cfg(target_arch = "wasm32")]
    if let Ok(callback) = js_sys::Reflect::get(&js_sys::global(), &"__wsorbEngineError".into())
        && let Some(callback) = callback.dyn_ref::<js_sys::Function>()
    {
        let _ = callback.call1(&JsValue::NULL, &message.into());
    }
}

fn install_panic_bridge() {
    std::panic::set_hook(Box::new(|info| fail(format!("Bevy stopped: {info}"))));
}

fn pipeline_label(descriptor: &PipelineDescriptor) -> &str {
    match descriptor {
        PipelineDescriptor::RenderPipelineDescriptor(descriptor) => descriptor.label.as_deref(),
        PipelineDescriptor::ComputePipelineDescriptor(descriptor) => descriptor.label.as_deref(),
        PipelineDescriptor::MeshPipelineDescriptor(descriptor) => descriptor.label.as_deref(),
    }
    .unwrap_or("unnamed pipeline")
}

fn observe_device(adapter: Res<RenderAdapterInfo>, device: Res<RenderDevice>) {
    RENDERER.with(|state| {
        let mut state = state.borrow_mut();
        state.adapter = Some(AdapterState {
            name: adapter.name.clone(),
            backend: format!("{:?}", adapter.backend),
            device_type: format!("{:?}", adapter.device_type),
            driver: adapter.driver.clone(),
            features: format!("{:?}", device.features()),
        });
        if !state.ready && state.error.is_none() {
            state.stage = StartupStage::PreparingView;
        }
    });
}

fn observe_frame(
    cache: Res<PipelineCache>,
    queue: Res<RenderQueue>,
    maps: Option<Res<crate::scene::SurfaceMaps>>,
    gpu_images: Res<RenderAssets<GpuImage>>,
    cameras: Query<(&ExtractedCamera, Option<&ViewTarget>, &PrimaryGameFrame)>,
    windows: Query<(MainEntity, &ExtractedWindow), With<PrimaryWindow>>,
) {
    let maps_ready = match maps.as_ref().map(|maps| maps.loading_status()) {
        Some(Err(error)) => {
            fail(error.to_owned());
            return;
        }
        Some(Ok(ready)) => ready,
        None => false,
    };
    let maps_gpu_ready = maps_ready
        && maps
            .as_ref()
            .is_some_and(|maps| maps.gpu_ready(&gpu_images));
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
    let initial_assets_ready = maps_gpu_ready && pending == 0 && pending_labels.is_empty();
    let mut viewport = None;
    let mut target_size = None;
    let mut world_draws = 0;
    let mut planetary_draw_mask = 0;
    let mut presented = false;
    let mut stage = StartupStage::PreparingView;
    if let Ok((camera, target, frame)) = cameras.single() {
        viewport = camera.physical_viewport_size.map(|size| size.to_array());
        target_size = camera.physical_target_size.map(|size| size.to_array());
        world_draws = frame.world_draws.load(Ordering::Relaxed);
        planetary_draw_mask = frame.planetary_draw_mask.load(Ordering::Relaxed);
        stage = if target.is_none() || viewport.is_none() {
            StartupStage::PreparingView
        } else if world_draws == 0
            || planetary_draw_mask != crate::scene::PlanetarySurface::ALL_DRAW_BITS
        {
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
            presented = world_draws > 0
                && initial_assets_ready
                && planetary_draw_mask == crate::scene::PlanetarySurface::ALL_DRAW_BITS;
        }
    }
    RENDERER.with(|state| {
        let mut state = state.borrow_mut();
        state.compiled_pipelines = compiled;
        state.pending_pipelines = pending;
        state.pending_pipeline_labels = pending_labels;
        state.surface_maps_ready = maps_ready;
        state.surface_maps_gpu_ready = maps_gpu_ready;
        state.rendered_frames = state.rendered_frames.saturating_add(1);
        state.primary_world_draws = world_draws;
        state.primary_planetary_draw_mask = planetary_draw_mask;
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
            } else if initial_assets_ready {
                stage
            } else {
                StartupStage::PreparingView
            };
        }
        state.refresh_ready();
    });
}

const BASELINE_PBR_MARKER: &str = "// wsorb: Bevy 0.20 opaque PBR bindings inlined\n";
const PBR_TEXTURE_BINDINGS: &[(&str, &str)] = &[
    ("base_color_texture", "base_color_sampler"),
    ("specular_texture", "specular_sampler"),
    ("specular_tint_texture", "specular_tint_sampler"),
    ("emissive_texture", "emissive_sampler"),
    ("metallic_roughness_texture", "metallic_roughness_sampler"),
    ("clearcoat_texture", "clearcoat_sampler"),
    ("clearcoat_roughness_texture", "clearcoat_roughness_sampler"),
    (
        "specular_transmission_texture",
        "specular_transmission_sampler",
    ),
    ("thickness_texture", "thickness_sampler"),
    (
        "diffuse_transmission_texture",
        "diffuse_transmission_sampler",
    ),
    ("occlusion_texture", "occlusion_sampler"),
    ("normal_map_texture", "normal_map_sampler"),
    ("clearcoat_normal_texture", "clearcoat_normal_sampler"),
    ("anisotropy_texture", "anisotropy_sampler"),
];

fn baseline_pbr_bindings(path: &str) -> Option<&'static [(&'static str, &'static str)]> {
    match path {
        "embedded://bevy_pbr/render/pbr_fragment.wesl" => Some(PBR_TEXTURE_BINDINGS),
        "embedded://bevy_pbr/render/pbr_prepass_functions.wesl" => Some(&PBR_TEXTURE_BINDINGS[..1]),
        "embedded://bevy_pbr/render/pbr_prepass.wesl" => Some(&PBR_TEXTURE_BINDINGS[11..12]),
        _ => None,
    }
}

/// Token spans suffice for the pinned material alias pattern. Comments and
/// quoted text are never identifiers, so replacement cannot alter their words
/// or accidentally match `textureSample`, imports or longer variable names.
fn pbr_token_spans(source: &str) -> Result<Vec<Range<usize>>, String> {
    let bytes = source.as_bytes();
    let mut tokens = Vec::new();
    let mut index = 0;
    while index < bytes.len() {
        if bytes[index].is_ascii_whitespace() {
            index += 1;
        } else if bytes[index..].starts_with(b"//") {
            while index < bytes.len() && bytes[index] != b'\n' {
                index += 1;
            }
        } else if bytes[index..].starts_with(b"/*") {
            index += 2;
            let mut depth = 1;
            while index < bytes.len() && depth > 0 {
                if bytes[index..].starts_with(b"/*") {
                    depth += 1;
                    index += 2;
                } else if bytes[index..].starts_with(b"*/") {
                    depth -= 1;
                    index += 2;
                } else {
                    index += 1;
                }
            }
            if depth != 0 {
                return Err("Unterminated comment in embedded PBR shader".into());
            }
        } else {
            let start = index;
            if bytes[index].is_ascii_alphabetic() || bytes[index] == b'_' {
                index += 1;
                while index < bytes.len()
                    && (bytes[index].is_ascii_alphanumeric() || bytes[index] == b'_')
                {
                    index += 1;
                }
            } else if bytes[index] == b'"' {
                index += 1;
                let mut closed = false;
                while index < bytes.len() {
                    if bytes[index] == b'\\' {
                        index += 2;
                    } else if bytes[index] == b'"' {
                        index += 1;
                        closed = true;
                        break;
                    } else {
                        index += 1;
                    }
                }
                if !closed {
                    return Err("Unterminated quoted text in embedded PBR shader".into());
                }
            } else {
                index += source[index..].chars().next().unwrap().len_utf8();
            }
            tokens.push(start..index);
        }
    }
    Ok(tokens)
}

fn pbr_alias_declarations(texture: &str, sampler: &str) -> (String, &'static str, &'static str) {
    let (texture_alias, sampler_alias) = if texture == "clearcoat_normal_texture" {
        ("clearcoat_texture", "clearcoat_sampler")
    } else {
        ("texture", "sampler_")
    };
    (
        format!(
            "@if(BINDLESS) let {texture_alias} = bindless_textures_2d[material_indices[slot].{texture}];
             @else let {texture_alias} = pbr_bindings::{texture};
             @if(BINDLESS) let {sampler_alias} = bindless_samplers_filtering[material_indices[slot].{sampler}];
             @else let {sampler_alias} = pbr_bindings::{sampler};"
        ),
        texture_alias,
        sampler_alias,
    )
}

fn inline_pbr_alias_uses(
    body: &str,
    aliases: (&str, &str),
    bindings: (&str, &str),
) -> Result<String, String> {
    let mut output = String::with_capacity(body.len());
    let mut cursor = 0;
    let mut uses = [0, 0];
    for span in pbr_token_spans(body)? {
        let replacement = match &body[span.clone()] {
            token if token == aliases.0 => {
                uses[0] += 1;
                Some(bindings.0)
            }
            token if token == aliases.1 => {
                uses[1] += 1;
                Some(bindings.1)
            }
            _ => None,
        };
        if let Some(replacement) = replacement {
            output.push_str(&body[cursor..span.start]);
            output.push_str(replacement);
            cursor = span.end;
        }
    }
    if uses[0] == 0 || uses[0] != uses[1] {
        return Err(
            "Embedded PBR texture/sampler uses changed; compatibility rewrite refused".into(),
        );
    }
    output.push_str(&body[cursor..]);
    Ok(output)
}

fn baseline_pbr_source(source: &str, bindings: &[(&str, &str)]) -> Result<Option<String>, String> {
    if source.starts_with(BASELINE_PBR_MARKER) {
        return Ok(None);
    }
    let mut output = source.to_owned();
    for &(texture, sampler) in bindings {
        let (declarations, texture_alias, sampler_alias) = pbr_alias_declarations(texture, sampler);
        let pattern = pbr_token_spans(&declarations)?;
        let tokens = pbr_token_spans(&output)?;
        let matches = tokens
            .windows(pattern.len())
            .enumerate()
            .filter(|(_, window)| {
                window.iter().zip(&pattern).all(|(actual, expected)| {
                    output[actual.clone()] == declarations[expected.clone()]
                })
            })
            .map(|(index, _)| index)
            .collect::<Vec<_>>();
        let [start_token] = matches.as_slice() else {
            return Err(format!(
                "Expected one pinned Bevy 0.20 alias block for {texture}; found {}",
                matches.len()
            ));
        };
        let after_aliases = *start_token + pattern.len();
        let mut depth = 0;
        let mut end = None;
        for token in &tokens[after_aliases..] {
            match &output[token.clone()] {
                "{" => depth += 1,
                "}" if depth == 0 => {
                    end = Some(token.start);
                    break;
                }
                "}" => depth -= 1,
                _ => {}
            }
        }
        let end = end.ok_or("Embedded PBR alias block has no enclosing scope")?;
        let start = tokens[*start_token].start;
        let body_start = tokens[after_aliases - 1].end;
        let body = &output[body_start..end];
        let aliases = (texture_alias, sampler_alias);
        let regular = inline_pbr_alias_uses(
            body,
            aliases,
            (
                &format!("pbr_bindings::{texture}"),
                &format!("pbr_bindings::{sampler}"),
            ),
        )?;
        let bindless = inline_pbr_alias_uses(
            body,
            aliases,
            (
                &format!("bindless_textures_2d[material_indices[slot].{texture}]"),
                &format!("bindless_samplers_filtering[material_indices[slot].{sampler}]"),
            ),
        )?;
        // WESL conditional blocks select exactly one copy before WGSL is sent
        // to the browser. Preserve both binding models and all original UV,
        // bias, alpha-discard and normal-mapping calculations in that scope.
        output.replace_range(
            start..end,
            &format!("@if(BINDLESS) {{{bindless}\n}}\n@else {{{regular}\n}}\n"),
        );
    }
    let tokens = pbr_token_spans(&output)?;
    if tokens.windows(3).any(|window| {
        &output[window[0].clone()] == "let"
            && matches!(
                &output[window[1].clone()],
                "texture" | "sampler_" | "clearcoat_texture" | "clearcoat_sampler"
            )
            && &output[window[2].clone()] == "="
    }) {
        return Err("Unrecognized opaque alias remains in embedded PBR shader".into());
    }
    Ok(Some(format!("{BASELINE_PBR_MARKER}{output}")))
}

fn prepare_baseline_pbr_shaders(mut shaders: ResMut<Assets<Shader>>, mut commands: Commands) {
    // Bevy 0.20's three embedded material modules alias texture/sampler values.
    // Those lets require WGSL's optional texture_and_sampler_let language
    // feature. Inline only the known resource identifiers, not global strings.
    // https://developer.chrome.com/blog/new-in-webgpu-146
    let pending = shaders
        .iter()
        .filter_map(|(id, shader)| {
            baseline_pbr_bindings(&shader.path).map(|bindings| (id, bindings))
        })
        .collect::<Vec<_>>();
    for (id, bindings) in pending {
        let shader = shaders.get(id).unwrap();
        let path = shader.path.clone();
        let result = match &shader.source {
            Source::Wesl(source) => baseline_pbr_source(source, bindings),
            _ => Err("Expected WESL source for pinned Bevy 0.20 material module".into()),
        };
        match result {
            Ok(Some(source)) => {
                shaders.get_mut(id).unwrap().source = Source::Wesl(Cow::Owned(source))
            }
            Ok(None) => {}
            Err(error) => {
                fail(format!("Baseline PBR shader {path} failed: {error}"));
                commands.write_message(AppExit::error());
                return;
            }
        }
    }
}

fn browser_render_settings() -> WgpuSettings {
    // Request the common WebGPU baseline, not every optional feature advertised
    // by one adapter. Bevy selects its supported shader paths from this device.
    WgpuSettings {
        priority: WgpuSettingsPriority::WebGPU,
        features: WgpuFeatures::empty(),
        ..default()
    }
}

pub fn start(canvas_selector: &str) -> Result<(), JsValue> {
    if RENDERER.with(|state| state.borrow().started) {
        return Err(StartupError::RendererAlreadyStarted.into_js());
    }
    // Validate the actual browser surface before committing startup. Hold no
    // renderer borrow across browser calls or error reporting.
    #[cfg(target_arch = "wasm32")]
    browser_surface::claim_for_bevy(canvas_selector).map_err(StartupError::into_js)?;
    RENDERER.with(|state| state.borrow_mut().started = true);
    // Record construction/plugin failures too, after ownership is committed.
    install_panic_bridge();
    let mut app = App::new();
    app.insert_resource(ClearColor(Color::BLACK))
        .init_resource::<crate::PlaybackState>()
        .add_plugins(
            DefaultPlugins
                .set(RenderPlugin {
                    render_creation: browser_render_settings().into(),
                    ..default()
                })
                .set(WindowPlugin {
                    primary_window: Some(Window {
                        canvas: Some(canvas_selector.into()),
                        fit_canvas_to_parent: true,
                        prevent_default_event_handling: false,
                        ..default()
                    }),
                    ..default()
                }),
        )
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
                EngineSet::Reports,
                EngineSet::Camera,
                EngineSet::Scene,
                EngineSet::Hud,
            )
                .chain(),
        )
        .add_systems(Startup, crate::scene::setup)
        // Patch loaded embedded source before asset events reach shader-cache
        // extraction. Hot reloads follow the same path; no original textured
        // shader variant is submitted before its resource aliases are inlined.
        .add_systems(
            PostUpdate,
            prepare_baseline_pbr_shaders.before(AssetEventSystems),
        )
        .add_systems(
            Update,
            crate::session::advance_world.in_set(EngineSet::Runtime),
        )
        .add_systems(Update, crate::scene::sync_scene.in_set(EngineSet::Scene));
    crate::camera::install(&mut app);
    crate::sensors::install(&mut app);
    crate::navigation::install(&mut app);
    crate::telemetry::install(&mut app);
    crate::thrusters::install(&mut app);
    crate::hud::install(&mut app);
    crate::launchpad::install(&mut app);
    crate::scene::install_body_axes(&mut app);
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
    install_panic_bridge();
    app.run();
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[cfg(not(target_arch = "wasm32"))]
    #[test]
    fn panic_bridge_does_not_reborrow_renderer_state() {
        RENDERER.with(|state| {
            let _borrow = state.borrow_mut();
            fail("Panic while renderer state is borrowed".into());
        });
    }

    fn material_alias_fixture(texture: &str, sampler: &str, body: &str) -> String {
        let (declarations, _, _) = pbr_alias_declarations(texture, sampler);
        format!("fn material_sample() {{\n{declarations}\n{body}\n}}")
    }

    #[test]
    fn embedded_pbr_compatibility_is_limited_to_three_pinned_material_modules() {
        assert_eq!(
            baseline_pbr_bindings("embedded://bevy_pbr/render/pbr_fragment.wesl")
                .unwrap()
                .len(),
            14
        );
        assert_eq!(
            baseline_pbr_bindings("embedded://bevy_pbr/render/pbr_prepass_functions.wesl"),
            Some(&PBR_TEXTURE_BINDINGS[..1])
        );
        assert_eq!(
            baseline_pbr_bindings("embedded://bevy_pbr/render/pbr_prepass.wesl"),
            Some(&PBR_TEXTURE_BINDINGS[11..12])
        );
        assert!(
            baseline_pbr_bindings("embedded://bevy_core_pipeline/skybox/skybox.wesl").is_none()
        );
        assert!(baseline_pbr_bindings("textures/pbr_fragment.wesl").is_none());
    }

    #[test]
    fn opaque_pbr_aliases_are_inlined_for_both_binding_models_without_changing_sampling() {
        let source = material_alias_fixture(
            "base_color_texture",
            "base_color_sampler",
            "@if(STANDARD_MATERIAL_BASE_COLOR_UV_B) let coords = uv_b;
             @else let coords = uv;
             @if(MESHLET_MESH_MATERIAL_PASS)
             pbr_input.material.base_color *= textureSampleGrad(texture, sampler_, coords, bias.ddx_uv, bias.ddy_uv);
             @else
             pbr_input.material.base_color *= textureSampleBias(texture, sampler_, coords, bias.mip_bias);",
        );
        let output = baseline_pbr_source(&source, &PBR_TEXTURE_BINDINGS[..1])
            .unwrap()
            .unwrap();
        assert!(!output.contains("let texture ="));
        assert!(!output.contains("let sampler_ ="));
        assert!(output.contains(
            "textureSampleBias(pbr_bindings::base_color_texture, pbr_bindings::base_color_sampler, coords, bias.mip_bias)"
        ));
        assert!(output.contains(
            "textureSampleGrad(bindless_textures_2d[material_indices[slot].base_color_texture], bindless_samplers_filtering[material_indices[slot].base_color_sampler], coords, bias.ddx_uv, bias.ddy_uv)"
        ));
        assert_eq!(output.matches("let coords = uv_b;").count(), 2);
        assert_eq!(output.matches("@if(MESHLET_MESH_MATERIAL_PASS)").count(), 2);
        assert_eq!(
            baseline_pbr_source(&output, &PBR_TEXTURE_BINDINGS[..1]).unwrap(),
            None
        );
    }

    #[test]
    fn prepass_multiline_samples_and_nested_alpha_scopes_keep_their_arguments() {
        let source = material_alias_fixture(
            "base_color_texture",
            "base_color_sampler",
            "output_color *= textureSampleBias(\n texture,\n sampler_,\n uv,\n view.mip_bias\n );
             @if(ALPHA_TO_COVERAGE) { if cutoff > 0.0 { discard; } }",
        );
        let output = baseline_pbr_source(&source, &PBR_TEXTURE_BINDINGS[..1])
            .unwrap()
            .unwrap();
        assert!(
            output
                .contains("pbr_bindings::base_color_texture,\n pbr_bindings::base_color_sampler,")
        );
        assert_eq!(output.matches("view.mip_bias").count(), 2);
        assert_eq!(output.matches("if cutoff > 0.0 { discard; }").count(), 2);
    }

    #[test]
    fn pbr_binding_rewrite_does_not_replace_comments_or_longer_identifiers() {
        let source = material_alias_fixture(
            "base_color_texture",
            "base_color_sampler",
            "// texture sampler_ { }\n /* nested /* texture */ sampler_ */
             let texture_sample_count = 1;
             let sampled = textureSampleBias(texture, sampler_, uv, 0.0);",
        );
        let output = baseline_pbr_source(&source, &PBR_TEXTURE_BINDINGS[..1])
            .unwrap()
            .unwrap();
        assert_eq!(output.matches("// texture sampler_ { }").count(), 2);
        assert_eq!(
            output
                .matches("/* nested /* texture */ sampler_ */")
                .count(),
            2
        );
        assert_eq!(output.matches("let texture_sample_count = 1;").count(), 2);
        assert!(!output.contains("let pbr_bindings::"));
    }

    #[test]
    fn changed_or_unused_upstream_shader_aliases_fail_instead_of_becoming_a_noop() {
        let absent = baseline_pbr_source("fn fragment() {}", &PBR_TEXTURE_BINDINGS[..1]);
        assert!(absent.is_err());
        let unused = material_alias_fixture("base_color_texture", "base_color_sampler", "return;");
        assert!(baseline_pbr_source(&unused, &PBR_TEXTURE_BINDINGS[..1]).is_err());
        let changed = material_alias_fixture(
            "base_color_texture",
            "base_color_sampler",
            "let sampled = textureSampleBias(texture, sampler_, uv, 0.0);",
        )
        .replace(
            "pbr_bindings::base_color_texture",
            "pbr_bindings::different_texture",
        );
        assert!(baseline_pbr_source(&changed, &PBR_TEXTURE_BINDINGS[..1]).is_err());
        assert!(pbr_token_spans("/* unclosed").is_err());
    }

    #[test]
    fn browser_renderer_does_not_enable_optional_adapter_features() {
        let settings = browser_render_settings();
        assert!(matches!(settings.priority, WgpuSettingsPriority::WebGPU));
        assert!(settings.features.is_empty());
        assert!(settings.adapter_name.is_none());
        assert!(!settings.force_fallback_adapter);
    }

    #[test]
    fn first_shader_error_is_not_replaced_by_invalid_module_fallout() {
        let mut state = RendererState::default();
        assert!(state.record_failure("WGSL compiler: unsupported declaration".into()));
        assert!(!state.record_failure("Fragment module is invalid".into()));
        assert_eq!(
            state.error.as_deref(),
            Some("WGSL compiler: unsupported declaration")
        );
        assert!(!state.ready);
        assert!(state.stage == StartupStage::Failed);
    }

    #[test]
    fn readiness_requires_gpu_completion_not_pipeline_or_frame_counts() {
        let mut state = RendererState {
            rendered_frames: 100_000,
            compiled_pipelines: 100,
            submitted_primary_frames: 1,
            surface_maps_ready: true,
            surface_maps_gpu_ready: true,
            primary_planetary_draw_mask: crate::scene::PlanetarySurface::ALL_DRAW_BITS,
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
            surface_maps_ready: true,
            surface_maps_gpu_ready: true,
            primary_planetary_draw_mask: crate::scene::PlanetarySurface::ALL_DRAW_BITS,
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
            surface_maps_ready: true,
            surface_maps_gpu_ready: true,
            primary_planetary_draw_mask: crate::scene::PlanetarySurface::ALL_DRAW_BITS,
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

    #[test]
    fn initial_readiness_requires_decoded_uploaded_maps_and_complete_pipelines() {
        let mut state = RendererState {
            submitted_primary_frames: 1,
            primary_planetary_draw_mask: crate::scene::PlanetarySurface::ALL_DRAW_BITS,
            ..default()
        };
        state.completion.store(true, Ordering::Release);
        state.refresh_ready();
        assert!(!state.ready);
        state.surface_maps_ready = true;
        state.refresh_ready();
        assert!(!state.ready);
        state.surface_maps_gpu_ready = true;
        state.pending_pipelines = 1;
        state.refresh_ready();
        assert!(!state.ready);
        state.pending_pipelines = 0;
        state.pending_pipeline_labels.push("loading shader".into());
        state.refresh_ready();
        assert!(!state.ready);
        state.pending_pipeline_labels.clear();
        state.refresh_ready();
        assert!(state.ready);
    }

    #[test]
    fn a_prop_only_or_single_planet_frame_cannot_finish_startup() {
        let mut state = RendererState {
            submitted_primary_frames: 1,
            surface_maps_ready: true,
            surface_maps_gpu_ready: true,
            ..default()
        };
        state.completion.store(true, Ordering::Release);
        state.refresh_ready();
        assert!(!state.ready);
        state.primary_planetary_draw_mask = crate::scene::PlanetarySurface::Phobos.draw_bit();
        state.refresh_ready();
        assert!(!state.ready);
        state.primary_planetary_draw_mask |= crate::scene::PlanetarySurface::Mars.draw_bit();
        state.refresh_ready();
        assert!(state.ready);
    }
}

#[cfg(all(test, not(target_arch = "wasm32")))]
mod shader_tests;
