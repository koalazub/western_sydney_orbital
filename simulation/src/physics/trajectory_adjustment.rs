use super::*;
use crate::model::{
    DebrisTrajectoryAdjustment, DebrisTrajectoryAdjustmentState, DebrisTrajectoryAdjustmentSummary,
};

const MAX_EXACT_JSON_INTEGER: u64 = (1_u64 << 53) - 1;
const TICK_ROUNDING_TOLERANCE: f64 = 1.0e-8;

pub(super) fn initial_state(
    experiment: Option<ExperimentSettings>,
) -> Option<DebrisTrajectoryAdjustmentState> {
    experiment
        .filter(|experiment| !experiment.debris.trajectory_adjustment.is_off())
        .map(|_| DebrisTrajectoryAdjustmentState {
            version: settings().debris_trajectory_adjustment.version,
            iteration: 0,
            applied_count: 0,
        })
}

fn completed_ticks(time_s: f64) -> Option<u64> {
    let ticks = time_s * settings().debris_trajectory_adjustment.global_rate_hz;
    if !ticks.is_finite() || ticks < 0.0 || ticks > MAX_EXACT_JSON_INTEGER as f64 {
        return None;
    }
    // Ordinary fixed-step sums can fall a few ulps short of an exact tick.
    // This tolerance is in ticks, never a wall-clock catch-up allowance.
    let rounded = ticks.round();
    let ticks = if (ticks - rounded).abs() <= TICK_ROUNDING_TOLERANCE {
        rounded
    } else {
        ticks.floor()
    };
    Some(ticks as u64)
}

pub(super) fn validate_state(state: &Snapshot) -> Result<(), String> {
    let mode = state
        .experiment_settings
        .map_or(DebrisTrajectoryAdjustment::Off, |experiment| {
            experiment.debris.trajectory_adjustment
        });
    match (mode, state.debris_trajectory_adjustment_state) {
        (DebrisTrajectoryAdjustment::Off, None) => Ok(()),
        (DebrisTrajectoryAdjustment::Off, Some(_)) => {
            Err("disabled debris trajectory adjustment cannot carry cursor history".into())
        }
        (DebrisTrajectoryAdjustment::TowardLander, None) => Err(
            "enabled debris trajectory adjustment requires its archived cursor; configure a new flight instead of inferring history"
                .into(),
        ),
        (DebrisTrajectoryAdjustment::TowardLander, Some(cursor)) => {
            if cursor.version != settings().debris_trajectory_adjustment.version
                || cursor.iteration > MAX_EXACT_JSON_INTEGER
                || cursor.applied_count > cursor.iteration
                || completed_ticks(state.time_s) != Some(cursor.iteration)
                || (state.experiment_settings.is_some_and(|experiment| experiment.debris.count == 0)
                    && cursor.applied_count != 0)
            {
                return Err("invalid deterministic debris trajectory adjustment cursor".into());
            }
            Ok(())
        }
    }
}

/// Plan only this physical substep. A restored clock cannot trigger an unbounded
/// history replay, even if the internal method is called without restore checks.
fn due_ticks(time_s: f64, h: f64, iteration: u64) -> Option<u64> {
    if !h.is_finite()
        || h <= 0.0
        || h > settings().integration.fixed_step_s + f64::EPSILON
        || completed_ticks(time_s)? != iteration
    {
        return None;
    }
    let end = time_s + h;
    if !end.is_finite() || end <= time_s {
        return None;
    }
    let due = completed_ticks(end)?.checked_sub(iteration)?;
    let maximum = (settings().integration.fixed_step_s
        * settings().debris_trajectory_adjustment.global_rate_hz)
        .ceil() as u64
        + 1;
    (due <= maximum).then_some(due)
}

impl Simulation {
    /// Public aggregate only. The cursor and individual rock truth stay in the
    /// archive, not in the planner's observation or this read-only summary.
    pub fn debris_trajectory_adjustment(&self) -> DebrisTrajectoryAdjustmentSummary {
        DebrisTrajectoryAdjustmentSummary {
            mode: self
                .state
                .experiment_settings
                .map_or(DebrisTrajectoryAdjustment::Off, |experiment| {
                    experiment.debris.trajectory_adjustment
                }),
            constants: settings().debris_trajectory_adjustment.clone(),
            applied_count: self
                .state
                .debris_trajectory_adjustment_state
                .map_or(0, |cursor| cursor.applied_count),
        }
    }

    pub(super) fn apply_debris_trajectory_adjustment(&mut self, h: f64) {
        let Some(experiment) = self.state.experiment_settings else {
            return;
        };
        if experiment.debris.trajectory_adjustment.is_off()
            || self.state.mission.status.is_terminal()
        {
            return;
        }
        let Some(mut cursor) = self.state.debris_trajectory_adjustment_state else {
            // Never silently activate guidance on an old archive.
            return;
        };
        let Some(due) = due_ticks(self.state.time_s, h, cursor.iteration) else {
            return;
        };
        let lander_position = self
            .handles
            .get(&EntityId::Lander)
            .and_then(|handle| self.bodies.get(*handle))
            .map(|body| body.translation());
        let config = &settings().debris_trajectory_adjustment;
        let delta_v = config.typical_speed_mps * config.delta_v_factor;
        for _ in 0..due {
            // The source selects iteration % numDebris once per global loop,
            // including rocks within the cutoff. Stable IDs preserve that order
            // even if an archive reorders its asset array or a rock recycles.
            let selected = (experiment.debris.count > 0).then(|| {
                EntityId::Debris((cursor.iteration % experiment.debris.count as u64 + 1) as u32)
            });
            cursor.iteration += 1;
            let Some((id, target)) = selected.zip(lander_position) else {
                continue;
            };
            let Some(body) = self
                .handles
                .get(&id)
                .and_then(|handle| self.bodies.get_mut(*handle))
                .filter(|body| body.is_dynamic())
            else {
                continue;
            };
            let toward = target - body.translation();
            let distance = toward.length();
            let mass = body.mass();
            if !distance.is_finite()
                || distance <= config.cutoff_m
                || !mass.is_finite()
                || mass <= 0.0
            {
                continue;
            }
            // Algebraically the source's t_intercept=d/v_typical and
            // factor*(v_target-v_debris) are a 0.02 m/s kick toward the lander.
            // Apply a real COM impulse, not a velocity reset or a spin constraint.
            let change = toward / distance * delta_v;
            let impulse = change * mass;
            let velocity = body.linvel() + change;
            if !impulse.is_finite()
                || !velocity.is_finite()
                || velocity
                    .to_array()
                    .into_iter()
                    .any(|component| component.abs() > settings().limits.velocity_mps)
            {
                continue;
            }
            body.apply_impulse(impulse, true);
            cursor.applied_count += 1;
        }
        self.state.debris_trajectory_adjustment_state = Some(cursor);
    }
}

#[cfg(test)]
#[path = "trajectory_adjustment_tests.rs"]
mod tests;
