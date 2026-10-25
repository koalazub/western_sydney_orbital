//! Animated debug exhaust, not a radiometric exhaust simulation. These marks
//! never enter mounted RGB sensor feeds and never change vehicle or camera state.
mod particles;

use bevy::prelude::*;
use particles::{Emitter, Parameters, ParticleCloud};

#[derive(Resource)]
struct DebugExhaust(ParticleCloud);

impl Default for DebugExhaust {
    fn default() -> Self {
        let config = crate::scenario::settings().debug_thrust;
        Self(ParticleCloud::new(Parameters {
            max_particles: config.max_particles,
            particles_per_second_per_jet: config.particles_per_second_per_jet,
            particle_lifetime_s: config.particle_lifetime_s,
            exhaust_speed_mps: config.exhaust_speed_mps,
            particle_radius_m: config.particle_radius_m,
            spread_rad: config.spread_rad,
        }))
    }
}

pub(crate) fn install(app: &mut App) {
    app.init_resource::<DebugExhaust>().add_systems(
        Update,
        (advance, draw).chain().in_set(crate::EngineSet::Scene),
    );
}

fn advance_snapshot(cloud: &mut ParticleCloud, snapshot: &crate::model::Snapshot) {
    if let Some(ship) = snapshot
        .assets
        .iter()
        .find(|asset| asset.id == snapshot.mission.shuttle_id)
    {
        // This is the same applied-force/nozzle contract used by real physics.
        // Commands, planner previews and visual hull dimensions are not inputs.
        let jets = crate::propulsion::applied_jets(ship);
        cloud.advance(
            snapshot.seed,
            snapshot.time_s,
            Some(Emitter::from_entity(ship)),
            &jets,
        );
    } else {
        cloud.advance(snapshot.seed, snapshot.time_s, None, &[]);
    }
}

fn advance(mut exhaust: ResMut<DebugExhaust>) {
    advance_snapshot(&mut exhaust.0, &crate::snapshot());
}

fn draw(exhaust: Res<DebugExhaust>, rig: Res<crate::camera::CameraRig>, mut gizmos: Gizmos) {
    // Sensors::setup assigns default gizmos to layer 1. Mounted RGB cameras see
    // only layers 0/2, including while this overlay is visible in a debug view.
    if rig.mode.sensor().is_some() || !crate::scene::visual_settings().guides {
        return;
    }
    let cloud = &exhaust.0;
    let Some(now_s) = cloud.time_s() else {
        return;
    };
    let config = cloud.parameters();
    for particle in cloud.particles() {
        let age = ((now_s - particle.birth_s) / config.particle_lifetime_s).clamp(0.0, 1.0);
        let fade = (1.0 - age) as f32;
        let color = Color::srgba(
            0.40 + 0.48 * fade,
            0.65 + 0.30 * fade,
            1.0,
            0.8 * fade * fade,
        );
        let position_m = particle.position_at(now_s);
        let radius_m = config.particle_radius_m * (0.7 + 1.8 * age);
        let streak_m = (config.exhaust_speed_mps * 0.018).max(radius_m);
        let (side, up) = particles::cross_axes(particle.outward_world);
        // Three short lines per particle, at most 1,536 with the default cap.
        // Keep coordinates in f64 metres until conversion to render kilometres.
        let render = |point: bevy::math::DVec3| (point * 0.001).as_vec3();
        gizmos.line(
            render(position_m - particle.outward_world * streak_m),
            render(position_m),
            color,
        );
        gizmos.line(
            render(position_m - side * radius_m),
            render(position_m + side * radius_m),
            color,
        );
        gizmos.line(
            render(position_m - up * radius_m),
            render(position_m + up * radius_m),
            color,
        );
    }
}
