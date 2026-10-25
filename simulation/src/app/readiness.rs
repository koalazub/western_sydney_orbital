//! Primary-view draw identity, observed only after Bevy's material draw succeeds.
use crate::scene::PlanetarySurface;
use bevy::{
    ecs::{
        query::ROQueryItem,
        system::{SystemParamItem, lifetimeless::SQuery},
    },
    prelude::*,
    render::{
        Extract,
        render_phase::{
            PhaseItem, PhaseItemExtraIndex, RenderCommand, RenderCommandResult, TrackedRenderPass,
        },
        sync_world::{MainEntity, RenderEntity},
    },
};
use std::{
    ops::Range,
    sync::atomic::{AtomicU32, Ordering},
};

/// Replaced on every extraction, so a successful draw belongs to this frame.
#[derive(Component, Default)]
pub(super) struct PrimaryGameFrame {
    pub(super) world_draws: AtomicU32,
    pub(super) planetary_draw_mask: AtomicU32,
}

pub(super) fn extract_primary_view(
    mut commands: Commands,
    cameras: Extract<Query<RenderEntity, With<crate::camera::SimulationCamera>>>,
) {
    for camera in &cameras {
        commands.entity(camera).insert(PrimaryGameFrame::default());
    }
}

type PlanetarySurfaces = SQuery<(&'static MainEntity, &'static PlanetarySurface)>;

fn single_surface(
    main_entity: MainEntity,
    batch_range: &Range<u32>,
    extra_index: &PhaseItemExtraIndex,
    surfaces: &Query<(&MainEntity, &PlanetarySurface)>,
) -> Option<PlanetarySurface> {
    // PlanetarySurface requires NoAutomaticBatching. Bevy's CPU, direct GPU
    // and indirect GPU unbatchable paths all keep one MainEntity per draw.
    // Refuse grouped draws rather than crediting a multidraw representative.
    if batch_range.len() != 1 {
        return None;
    }
    match extra_index {
        PhaseItemExtraIndex::None | PhaseItemExtraIndex::DynamicOffset(_) => {}
        PhaseItemExtraIndex::IndirectParametersIndex {
            range,
            batch_set_index,
        } => {
            if range.len() != 1 || batch_set_index.is_some() {
                return None;
            }
        }
    }
    surfaces
        .iter()
        .find_map(|(entity, surface)| (*entity == main_entity).then_some(*surface))
}

pub(super) struct RecordWorldDraw;

impl<P: PhaseItem> RenderCommand<P> for RecordWorldDraw {
    type Param = PlanetarySurfaces;
    type ViewQuery = Option<&'static PrimaryGameFrame>;
    // Bevy 0.20 PBR phase items use Entity::PLACEHOLDER for the render entity.
    // Resolve their retained MainEntity against the actual extracted surfaces.
    type ItemQuery = ();

    fn render<'w>(
        item: &P,
        frame: ROQueryItem<'w, '_, Self::ViewQuery>,
        _entity: Option<ROQueryItem<'w, '_, Self::ItemQuery>>,
        surfaces: SystemParamItem<'w, '_, Self::Param>,
        _pass: &mut TrackedRenderPass<'w>,
    ) -> RenderCommandResult {
        // The registered tuple runs this only after DrawMaterial succeeds,
        // including its pipeline, bindings, GPU preprocessing and mesh draw.
        // Sensor image cameras have no PrimaryGameFrame marker.
        let extra_index = item.extra_index();
        let has_instances = match &extra_index {
            PhaseItemExtraIndex::None | PhaseItemExtraIndex::DynamicOffset(_) => {
                !item.batch_range().is_empty()
            }
            PhaseItemExtraIndex::IndirectParametersIndex { range, .. } => !range.is_empty(),
        };
        if let Some(frame) = frame
            && has_instances
        {
            frame.world_draws.fetch_add(1, Ordering::Relaxed);
            if let Some(surface) = single_surface(
                item.main_entity(),
                item.batch_range(),
                &extra_index,
                &surfaces,
            ) {
                frame
                    .planetary_draw_mask
                    .fetch_or(surface.draw_bit(), Ordering::Relaxed);
            }
        }
        RenderCommandResult::Success
    }
}

#[cfg(all(test, not(target_arch = "wasm32")))]
mod tests {
    // Real Bevy extraction and phase-classification contracts only. These tests
    // create no RenderDevice or pipelines, execute no draw, and set no draw bits.
    use super::*;
    use bevy::{
        camera::visibility::ViewVisibility,
        ecs::{
            schedule::{ScheduleLabel, SystemSet},
            system::SystemState,
        },
        pbr::{RenderMaterialInstances, RenderMeshInstances, extract_meshes_for_cpu_building},
        render::{
            ExtractSchedule, Render, RenderApp, RenderPlugin, RenderSystems,
            batching::{
                NoAutomaticBatching,
                gpu_preprocessing::{GpuPreprocessingMode, GpuPreprocessingSupport},
            },
            extract_component::ExtractComponentPlugin,
            extract_plugin::ExtractPlugin,
            material_bind_groups::RenderMaterialBindings,
            render_phase::BinnedRenderPhaseType,
            renderer::RenderDevice,
            view::RenderVisibilityRanges,
        },
    };

    fn extraction_app() -> App {
        let mut app = App::new();
        app.add_plugins(MinimalPlugins)
            .add_plugins(ExtractPlugin::<RenderApp>::new(
                |_, _| {},
                Render::base_schedule,
                Render.intern(),
                RenderSystems::ExtractCommands.intern(),
                RenderSystems::PostCleanup.intern(),
            ))
            .add_plugins(ExtractComponentPlugin::<PlanetarySurface>::default())
            .init_resource::<Assets<Mesh>>();
        let render = app.sub_app_mut(RenderApp);
        render.update_schedule = Some(Render.intern());
        render
            .insert_resource(RenderMeshInstances::CpuBuilding(default()))
            .init_resource::<RenderMaterialInstances>()
            .init_resource::<RenderMaterialBindings>()
            .init_resource::<RenderVisibilityRanges>()
            .add_systems(ExtractSchedule, extract_meshes_for_cpu_building);
        app
    }

    fn spawn_mesh_metadata(app: &mut App, surface: Option<PlanetarySurface>) -> Entity {
        let mesh = app
            .world_mut()
            .resource_mut::<Assets<Mesh>>()
            .add(Cuboid::new(1.0, 1.0, 1.0));
        let mut entity = app.world_mut().spawn((
            Mesh3d(mesh),
            GlobalTransform::IDENTITY,
            ViewVisibility::VISIBLE,
        ));
        if let Some(surface) = surface {
            entity.insert(surface);
        }
        entity.id()
    }

    #[test]
    fn cpu_pbr_extraction_keeps_only_surfaces_unbatchable_for_all_phase_modes() {
        let mut app = extraction_app();
        let phobos = spawn_mesh_metadata(&mut app, Some(PlanetarySurface::Phobos));
        let mars = spawn_mesh_metadata(&mut app, Some(PlanetarySurface::Mars));
        let prop = spawn_mesh_metadata(&mut app, None);
        app.update();

        assert!(app.world().entity(phobos).contains::<NoAutomaticBatching>());
        assert!(app.world().entity(mars).contains::<NoAutomaticBatching>());
        assert!(!app.world().entity(prop).contains::<NoAutomaticBatching>());
        assert!(!app.is_plugin_added::<RenderPlugin>());
        let render = app.sub_app(RenderApp).world();
        assert!(!render.contains_resource::<RenderDevice>());
        let instances = render.resource::<RenderMeshInstances>();
        for mode in [
            GpuPreprocessingMode::None,
            GpuPreprocessingMode::PreprocessingOnly,
            GpuPreprocessingMode::Culling,
        ] {
            let support = GpuPreprocessingSupport {
                max_supported_mode: mode,
            };
            for entity in [phobos, mars] {
                let mesh = instances.render_mesh_queue_data(entity.into()).unwrap();
                assert!(!mesh.should_batch());
                assert_eq!(
                    BinnedRenderPhaseType::mesh(mesh.should_batch(), &support),
                    BinnedRenderPhaseType::UnbatchableMesh,
                );
            }
            let mesh = instances.render_mesh_queue_data(prop.into()).unwrap();
            assert!(mesh.should_batch());
            assert_eq!(
                BinnedRenderPhaseType::mesh(mesh.should_batch(), &support),
                match mode {
                    GpuPreprocessingMode::None | GpuPreprocessingMode::PreprocessingOnly => {
                        BinnedRenderPhaseType::BatchableMesh
                    }
                    GpuPreprocessingMode::Culling => BinnedRenderPhaseType::MultidrawableMesh,
                },
            );
        }
    }

    #[test]
    fn extracted_main_identity_resolves_single_mesh_metadata_not_a_placeholder_or_group() {
        let mut app = extraction_app();
        let phobos = spawn_mesh_metadata(&mut app, Some(PlanetarySurface::Phobos));
        let mars = spawn_mesh_metadata(&mut app, Some(PlanetarySurface::Mars));
        let prop = spawn_mesh_metadata(&mut app, None);
        app.update();

        let render = app.sub_app_mut(RenderApp).world_mut();
        let mut state = SystemState::<PlanetarySurfaces>::new(render);
        let surfaces = state.get(render).unwrap();
        assert!(surfaces.get(Entity::PLACEHOLDER).is_err());
        for extra_index in [
            PhaseItemExtraIndex::None,
            PhaseItemExtraIndex::DynamicOffset(0),
            PhaseItemExtraIndex::IndirectParametersIndex {
                range: 7..8,
                batch_set_index: None,
            },
        ] {
            for (entity, expected) in [
                (phobos, PlanetarySurface::Phobos),
                (mars, PlanetarySurface::Mars),
            ] {
                assert_eq!(
                    single_surface(entity.into(), &(3..4), &extra_index, &surfaces)
                        .map(PlanetarySurface::draw_bit),
                    Some(expected.draw_bit()),
                );
            }
            for entity in [prop, Entity::PLACEHOLDER] {
                assert!(single_surface(entity.into(), &(3..4), &extra_index, &surfaces).is_none());
            }
            for range in [3..3, 3..5] {
                assert!(single_surface(phobos.into(), &range, &extra_index, &surfaces).is_none());
            }
        }
        for extra_index in [
            PhaseItemExtraIndex::IndirectParametersIndex {
                range: 7..7,
                batch_set_index: None,
            },
            PhaseItemExtraIndex::IndirectParametersIndex {
                range: 7..9,
                batch_set_index: None,
            },
            PhaseItemExtraIndex::IndirectParametersIndex {
                range: 7..8,
                batch_set_index: Some(default()),
            },
        ] {
            assert!(single_surface(phobos.into(), &(3..4), &extra_index, &surfaces).is_none());
        }
    }

    #[test]
    fn removed_surface_identity_is_not_retained_by_the_render_query() {
        let mut app = extraction_app();
        let phobos = spawn_mesh_metadata(&mut app, Some(PlanetarySurface::Phobos));
        let mars = spawn_mesh_metadata(&mut app, Some(PlanetarySurface::Mars));
        app.update();
        app.world_mut()
            .entity_mut(mars)
            .remove::<PlanetarySurface>();
        app.update();

        let render = app.sub_app_mut(RenderApp).world_mut();
        let mut state = SystemState::<PlanetarySurfaces>::new(render);
        let surfaces = state.get(render).unwrap();
        assert_eq!(surfaces.iter().count(), 1);
        assert!(
            single_surface(mars.into(), &(0..1), &PhaseItemExtraIndex::None, &surfaces).is_none()
        );
        assert_eq!(
            single_surface(
                phobos.into(),
                &(0..1),
                &PhaseItemExtraIndex::None,
                &surfaces
            )
            .map(PlanetarySurface::draw_bit),
            Some(PlanetarySurface::Phobos.draw_bit()),
        );
    }
}
