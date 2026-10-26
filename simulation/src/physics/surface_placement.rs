use super::*;
use crate::terrain::{
    LandingSite, landing_site_at_direction, mesh_surface_point_at_direction,
    normalized_surface_direction,
};
use rapier3d_f64::parry::{
    bounding_volume::{Aabb, BoundingVolume},
    query::{contact, intersection_test},
    shape::Shape,
};

struct RelocatedBody {
    asset_index: usize,
    handle: RigidBodyHandle,
    collider_handle: ColliderHandle,
    pose: Pose,
    collider_pose: Pose,
    shape: SharedShape,
    bounds: Aabb,
}

struct LaunchSiteMove {
    direction: [f64; 3],
    pad: RelocatedBody,
    lander: RelocatedBody,
    previous_pad_pose: Pose,
    target_distance_m: f64,
    relative_speed_mps: f64,
}

impl Simulation {
    /// Recorded local intervention, not a maneuver or a new trial. The session
    /// adapter must require an already-paused world and check its source context.
    /// Every fallible geometry/body check completes before any mutation here.
    pub fn move_launchpad(&mut self, surface_direction: [f64; 3]) -> Result<(), String> {
        let interventions = self.state.manual_interventions.checked_add(1).ok_or(
            "Local test intervention counter exhausted. Reset the run before moving the launchpad.",
        )?;
        let movement = self.prepare_launch_site_move(surface_direction)?;
        let previous = movement.previous_pad_pose;
        let next = movement.pad.pose;
        self.apply_launch_site_move(movement);
        self.state.manual_interventions = interventions;
        event(
            &mut self.state,
            EntityId::LandingPad,
            format!(
                "Local test: launchpad relocation from position {:?} m rotation {:?} to position {:?} m rotation {:?}; lander seated upright at rest; pad/lander contact caches and radar invalidated",
                previous.translation.to_array(),
                previous.rotation.to_array(),
                next.translation.to_array(),
                next.rotation.to_array(),
            ),
        );
        Ok(())
    }

    /// Called only on an uninstalled replacement after canonical debris births.
    /// A blocked custom start rejects Reset without changing the current trial.
    pub(super) fn reset_launch_site(&mut self, direction: [f64; 3]) -> Result<(), String> {
        let movement = self
            .prepare_launch_site_move(direction)
            .map_err(|error| format!("Reset could not retain the custom launch site: {error}"))?;
        let direction = movement.direction;
        // Compute with the same public helper used during archive validation,
        // rather than inferring trial history from a later airborne lander.
        let site = archived_launch_site(direction)?;
        let target = self
            .state
            .assets
            .iter()
            .find(|entity| entity.id == EntityId::ReturnShip)
            .ok_or("Reset unavailable: target entity missing.")?;
        let initial_distance = norm(sub(site.lander_position_m, target.position));
        self.apply_launch_site_move(movement);
        self.state.trial_start_launch_site_direction = Some(direction);
        self.state.mission.initial_distance_m = initial_distance;
        Ok(())
    }

    fn prepare_launch_site_move(&self, direction: [f64; 3]) -> Result<LaunchSiteMove, String> {
        let direction = normalized_surface_direction(direction)?;
        // Use the stored, normalized anchor for both commit and future Reset /
        // Restore so repeat normalization cannot change the recorded initial range.
        let site = landing_site_at_direction(direction)?;
        self.validate_launch_site_sources()?;
        let rotation = Rotation::from_xyzw(
            site.rotation[0],
            site.rotation[1],
            site.rotation[2],
            site.rotation[3],
        );
        let pad = self.relocation_candidate(EntityId::LandingPad, site.pad_position_m, rotation)?;
        let lander =
            self.relocation_candidate(EntityId::Lander, site.lander_position_m, rotation)?;
        let phobos_handle = self.handles[&EntityId::Phobos];
        let phobos = &self.bodies[phobos_handle];
        let [terrain_handle] = phobos.colliders() else {
            return Err(
                "Move launchpad unavailable: Phobos collider missing. Reset the run.".into(),
            );
        };
        let terrain = &self.colliders[*terrain_handle];
        let terrain_pose = self.actual_collider_pose(terrain)?;
        if terrain_pose != Pose::identity()
            || !std::ptr::eq(terrain.shape(), phobos_shape().as_ref())
        {
            return Err("Move launchpad unavailable: actual Phobos frame differs from its shared mesh. Reset the run.".into());
        }
        // Mesh intersection alone cannot detect a whole body below the surface.
        // Verify the shared mesh itself, including polar/edge roundoff where a
        // strict Rapier ray may miss a numerical seam between contact triangles.
        let surface_error = terrain.shape().distance_to_point(
            &terrain_pose,
            Vector::from_array(site.surface_position_m),
            false,
        );
        if !surface_error.is_finite() || surface_error > 1.0e-7 {
            return Err("Move launchpad unavailable: actual Phobos shape differs from the preview mesh. Reset the run.".into());
        }
        for candidate in [&pad, &lander] {
            let center = candidate.collider_pose.translation;
            if center == Vector::ZERO {
                return Err(
                    "Launchpad or lander would be inside Phobos. Try a different surface point."
                        .into(),
                );
            }
            let boundary = mesh_surface_point_at_direction(center.to_array()).map_err(
                |_| "Move launchpad unavailable: shared terrain containment query failed.",
            )?;
            if center.length() <= norm(boundary) {
                return Err(
                    "Launchpad or lander would be inside Phobos. Try a different surface point."
                        .into(),
                );
            }
        }
        // The old pad and ship must not reject their own replacement footprint.
        // Every other collider is queried at its actual current body/local pose,
        // even when Rapier has not propagated a dirty body into its cached pose.
        for (handle, other) in self.colliders.iter() {
            let other_pose = self.actual_collider_pose(other)?;
            let other_bounds = checked_bounds(other_pose, other.shape())?;
            if [pad.collider_handle, lander.collider_handle].contains(&handle)
                || !other.is_enabled()
            {
                continue;
            }
            let id = *self
                .collider_ids
                .get(&handle)
                .ok_or("Move launchpad unavailable: collider identity missing. Reset the run.")?;
            for candidate in [&pad, &lander] {
                if !candidate.bounds.intersects(&other_bounds) {
                    continue;
                }
                let intersects = intersection_test(
                    &candidate.collider_pose,
                    candidate.shape.as_ref(),
                    &other_pose,
                    other.shape(),
                )
                .map_err(
                    |_| "Move launchpad unavailable: actual shape intersection query unsupported.",
                )?;
                if !intersects.intersecting {
                    continue;
                }
                if id == EntityId::Phobos && candidate.handle == pad.handle {
                    // Intentional support contact is allowed; actual burial is
                    // not. The helper already fits every triangle in the full
                    // footprint, and this is the real collider's final check.
                    let support = contact(
                        &candidate.collider_pose,
                        candidate.shape.as_ref(),
                        &other_pose,
                        other.shape(),
                        0.0,
                    )
                    .map_err(
                        |_| "Move launchpad unavailable: terrain contact query unsupported.",
                    )?;
                    if support
                        .is_some_and(|contact| contact.dist.is_finite() && contact.dist >= -1.0e-7)
                    {
                        continue;
                    }
                }
                return Err(format!(
                    "Launchpad or seated lander intersects {id}. Try a different surface point."
                ));
            }
        }
        if intersection_test(
            &pad.collider_pose,
            pad.shape.as_ref(),
            &lander.collider_pose,
            lander.shape.as_ref(),
        )
        .map_err(|_| "Move launchpad unavailable: pad/lander intersection query unsupported.")?
        .intersecting
        {
            return Err(
                "Seated lander would intersect the launchpad. Try a different surface point."
                    .into(),
            );
        }
        let mars_shape = SharedShape::ball(self.state.environment.mars_radius_m);
        let mars_pose =
            Pose::from_translation(Vector::from_array(self.state.environment.mars_position_m));
        let mars_bounds = checked_bounds(mars_pose, mars_shape.as_ref())?;
        for candidate in [&pad, &lander] {
            if candidate.bounds.intersects(&mars_bounds)
                && intersection_test(
                    &candidate.collider_pose,
                    candidate.shape.as_ref(),
                    &mars_pose,
                    mars_shape.as_ref(),
                )
                .map_err(|_| "Move launchpad unavailable: Mars intersection query unsupported.")?
                .intersecting
            {
                return Err(
                    "Launchpad or seated lander intersects Mars. Try a different surface point."
                        .into(),
                );
            }
        }
        let target = &self.bodies[self.handles[&EntityId::ReturnShip]];
        let target_distance_m = norm(sub(
            lander.pose.translation.to_array(),
            target.translation().to_array(),
        ));
        let relative_speed_mps = norm(target.linvel().to_array());
        if !target_distance_m.is_finite() || !relative_speed_mps.is_finite() {
            return Err(
                "Move launchpad unavailable: target readback is not finite. Reset the run.".into(),
            );
        }
        Ok(LaunchSiteMove {
            direction,
            previous_pad_pose: *self.bodies[pad.handle].position(),
            pad,
            lander,
            target_distance_m,
            relative_speed_mps,
        })
    }

    fn validate_launch_site_sources(&self) -> Result<(), String> {
        let count = self.state.assets.len() + 1;
        if self.handles.len() != count
            || self.bodies.len() != count
            || self.colliders.len() != count
            || self.collider_ids.len() != count
            || !self.state.time_s.is_finite()
            || self.state.time_s < 0.0
            || !self.state.environment.mars_radius_m.is_finite()
            || self.state.environment.mars_radius_m <= 0.0
        {
            return Err(
                "Move launchpad unavailable: incomplete or invalid physical world. Reset the run."
                    .into(),
            );
        }
        let mut identities = HashSet::with_capacity(count);
        for (id, kind) in self
            .state
            .assets
            .iter()
            .map(|asset| (asset.id, Some(asset.kind)))
            .chain(std::iter::once((EntityId::Phobos, None)))
        {
            if !identities.insert(id) {
                return Err(
                    "Move launchpad unavailable: duplicate physical identity. Reset the run."
                        .into(),
                );
            }
            let body_handle = *self
                .handles
                .get(&id)
                .ok_or("Move launchpad unavailable: body missing. Reset the run.")?;
            let body = self
                .bodies
                .get(body_handle)
                .ok_or("Move launchpad unavailable: body missing. Reset the run.")?;
            let valid_type = match (id, kind) {
                (EntityId::LandingPad, Some(EntityKind::LandingPad)) | (EntityId::Phobos, None) => {
                    body.is_fixed()
                }
                (EntityId::ReturnShip, Some(EntityKind::ReturnShip)) => body.is_kinematic(),
                (EntityId::Lander, Some(EntityKind::Lander))
                | (EntityId::Debris(_), Some(EntityKind::Debris)) => body.is_dynamic(),
                _ => false,
            };
            if !valid_type
                || !body.is_enabled()
                || !body.linvel().is_finite()
                || !body.angvel().is_finite()
                || !body.user_force().is_finite()
                || !body.user_torque().is_finite()
                || body
                    .linvel()
                    .to_array()
                    .into_iter()
                    .any(|v| v.abs() > settings().limits.velocity_mps)
            {
                return Err(
                    "Move launchpad unavailable: invalid body state. Reset the run.".into(),
                );
            }
            let [collider_handle] = body.colliders() else {
                return Err("Move launchpad unavailable: collider missing. Reset the run.".into());
            };
            let collider = self
                .colliders
                .get(*collider_handle)
                .ok_or("Move launchpad unavailable: collider missing. Reset the run.")?;
            if collider.parent() != Some(body_handle)
                || self.collider_ids.get(collider_handle) != Some(&id)
                || !collider.is_enabled()
            {
                return Err("Move launchpad unavailable: collider identity or parent differs. Reset the run.".into());
            }
            let pose = *body.position();
            validate_pose(pose)?;
            let local = *collider.position_wrt_parent().ok_or(
                "Move launchpad unavailable: collider local transform missing. Reset the run.",
            )?;
            validate_pose(local)?;
            checked_bounds(pose * local, collider.shape())?;
        }
        for id in [EntityId::LandingPad, EntityId::Lander, EntityId::ReturnShip] {
            if !identities.contains(&id) {
                return Err(
                    "Move launchpad unavailable: pad, lander or target missing. Reset the run."
                        .into(),
                );
            }
        }
        Ok(())
    }

    fn relocation_candidate(
        &self,
        id: EntityId,
        position: [f64; 3],
        rotation: Rotation,
    ) -> Result<RelocatedBody, String> {
        let asset_index = self
            .state
            .assets
            .iter()
            .position(|asset| asset.id == id)
            .ok_or("Move launchpad unavailable: pad or lander entity missing. Reset the run.")?;
        let handle = self.handles[&id];
        let collider_handle = self.bodies[handle].colliders()[0];
        let collider = &self.colliders[collider_handle];
        let local = *collider.position_wrt_parent().ok_or(
            "Move launchpad unavailable: collider local transform missing. Reset the run.",
        )?;
        let pose = Pose::from_parts(Vector::from_array(position), rotation);
        validate_pose(pose)?;
        let collider_pose = pose * local;
        let shape = collider.shared_shape().clone();
        let bounds = checked_bounds(collider_pose, shape.as_ref())?;
        Ok(RelocatedBody {
            asset_index,
            handle,
            collider_handle,
            pose,
            collider_pose,
            shape,
            bounds,
        })
    }

    fn apply_launch_site_move(&mut self, movement: LaunchSiteMove) {
        let removed = [
            movement.pad.collider_handle,
            movement.lander.collider_handle,
        ];
        for candidate in [&movement.pad, &movement.lander] {
            let body = &mut self.bodies[candidate.handle];
            body.set_position(candidate.pose, true);
            body.set_linvel(Vector::ZERO, true);
            body.set_angvel(Vector::ZERO, true);
            body.reset_forces(false);
            body.reset_torques(false);
            let asset = &mut self.state.assets[candidate.asset_index];
            asset.position = body.translation().to_array();
            asset.rotation = body.rotation().to_array();
            asset.velocity = [0.0; 3];
            asset.angular_velocity_rad_s = [0.0; 3];
            asset.commanded_accel_mps2 = [0.0; 3];
            asset.commanded_angular_accel_rad_s2 = [0.0; 3];
            asset.thrust_n = [0.0; 3];
            asset.torque_nm = [0.0; 3];
            asset.thrust_application = ThrustApplication::CenterOfMass;
            if asset.id == EntityId::Lander {
                asset.status = EntityStatus::Landed;
            }
            // The same operation Rapier's propagation uses, restricted to the
            // two verified colliders rather than every dirty body in a new world.
            self.colliders[candidate.collider_handle].set_position(candidate.collider_pose);
        }
        self.last_actuator_application = None;
        let mut pair_events = Vec::new();
        self.broad.update(
            &self.integration,
            &self.colliders,
            &self.bodies,
            &[],
            &removed,
            &mut pair_events,
        );
        self.narrow.handle_user_changes(
            Some(&mut self.islands),
            &[],
            &removed,
            &mut self.colliders,
            &mut self.bodies,
            &(),
        );
        // A just-restored trial can have real recorded contact history before
        // Rapier has rebuilt its first manifolds. Keep unrelated history too,
        // rather than double-counting those contacts on the next normal step.
        self.touching.retain(|pair| {
            !pair.contains(&EntityId::LandingPad) && !pair.contains(&EntityId::Lander)
        });
        self.touching.extend(
            self.narrow
                .contact_pairs()
                .filter(|pair| pair.has_any_active_contact())
                .filter_map(|pair| {
                    Some(pair_key(
                        *self.collider_ids.get(&pair.collider1)?,
                        *self.collider_ids.get(&pair.collider2)?,
                    ))
                }),
        );
        self.state.active_contacts = self.touching.iter().copied().collect();
        self.state.active_contacts.sort();
        self.state.observations.clear();
        self.invalidate_sensor_samples();
        self.state.launch_site_direction = Some(movement.direction);
        self.state.mission.target_distance_m = movement.target_distance_m;
        self.state.mission.relative_speed_mps = movement.relative_speed_mps;
        // Do not assess or rescore here. Terminal outcomes, initial range, fuel,
        // damage, clock, tuning, RNG and every other body stay exactly as they were.
    }
}

fn validate_pose(pose: Pose) -> Result<(), String> {
    if !pose.translation.is_finite()
        || !pose.rotation.is_finite()
        || (pose.rotation.length_squared() - 1.0).abs() > 1.0e-6
        || pose
            .translation
            .to_array()
            .into_iter()
            .any(|v| v.abs() > settings().limits.position_m)
    {
        return Err(
            "Move launchpad unavailable: non-finite or out-of-bounds actual shape. Reset the run."
                .into(),
        );
    }
    Ok(())
}

fn checked_bounds(pose: Pose, shape: &dyn Shape) -> Result<Aabb, String> {
    validate_pose(pose)?;
    let bounds = shape.compute_aabb(&pose);
    if !bounds.mins.is_finite()
        || !bounds.maxs.is_finite()
        || (0..3).any(|axis| bounds.mins[axis] > bounds.maxs[axis])
    {
        return Err(
            "Move launchpad unavailable: non-finite actual shape bounds. Reset the run.".into(),
        );
    }
    Ok(bounds)
}

pub(super) fn archived_launch_site(direction: [f64; 3]) -> Result<LandingSite, String> {
    let normalized = normalized_surface_direction(direction)?;
    if direction
        .into_iter()
        .zip(normalized)
        .any(|(a, b)| (a - b).abs() > 1.0e-12)
    {
        return Err("saved launch site direction must be a finite unit vector".into());
    }
    landing_site_at_direction(direction)
}

pub(super) fn same_orientation(a: [f64; 4], b: [f64; 4]) -> bool {
    // q and -q encode the same active rotation. Do not admit a different heading.
    [1.0, -1.0].into_iter().any(|sign| {
        a.into_iter()
            .zip(b)
            .all(|(a, b)| (a - sign * b).abs() <= 1.0e-10)
    })
}
