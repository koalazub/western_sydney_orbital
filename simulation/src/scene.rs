//! Snapshot-only Bevy rendering; simulation state remains owned by the model.
mod body_axes;
mod meshes;
mod model;
mod presentation;
mod systems;
mod textures;

pub(crate) use model::PlanetarySurface;

pub(crate) use presentation::{
    IlluminationMode, VisualAction, VisualSettings, request_visual_action, visual_settings,
};
pub use systems::{setup, sync_scene};
pub(crate) use textures::SurfaceMaps;

pub(crate) fn install_body_axes(app: &mut bevy::prelude::App) {
    app.add_plugins(bevy::render::extract_component::ExtractComponentPlugin::<
        PlanetarySurface,
    >::default());
    body_axes::install(app);
    textures::install(app);
    presentation::install(app);
}
