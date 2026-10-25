//! Bounded-rate native debug sampling. This cache is not serialized as telemetry.

use crate::model::{
    DebrisDynamicsReport, DebrisFlowTuning, ExperimentSettings, FlightPreset, Snapshot,
};
use bevy::prelude::*;

#[derive(Clone, PartialEq)]
struct SampleKey {
    seed: u64,
    frame: String,
    preset: FlightPreset,
    experiment: Option<ExperimentSettings>,
    flow: DebrisFlowTuning,
    trial_revision: u32,
    interventions: u32,
}

impl SampleKey {
    fn new(snapshot: &Snapshot, trial_revision: u32) -> Self {
        Self {
            seed: snapshot.seed,
            frame: snapshot.frame.clone(),
            preset: snapshot.flight_preset,
            experiment: snapshot.experiment_settings,
            flow: snapshot.debris_flow(),
            trial_revision,
            interventions: snapshot.manual_interventions,
        }
    }
}

#[derive(Default, Resource)]
pub(super) struct DebrisSampleCache {
    key: Option<SampleKey>,
    previous_time_s: Option<f64>,
    sampled_wall_time_s: Option<f64>,
    pub sample: Option<DebrisDynamicsReport>,
    pub error: Option<String>,
    #[cfg(test)]
    pub samples_taken: usize,
}

impl DebrisSampleCache {
    pub fn refresh(&mut self, snapshot: &Snapshot, trial_revision: u32, wall_time_s: Option<f64>) {
        let key = SampleKey::new(snapshot, trial_revision);
        let changed = self.key.as_ref() != Some(&key)
            || self
                .previous_time_s
                .is_some_and(|before| snapshot.time_s < before);
        let due = wall_time_s
            .zip(self.sampled_wall_time_s)
            .is_some_and(|(now, before)| now - before >= 0.5 || now < before)
            || wall_time_s.is_some() && self.sampled_wall_time_s.is_none();
        self.previous_time_s = Some(snapshot.time_s);
        if !changed && !due {
            return;
        }
        self.key = Some(key);
        #[cfg(test)]
        {
            self.samples_taken += 1;
        }
        self.sampled_wall_time_s = wall_time_s;
        self.sample = None;
        self.error = None;
        match crate::session::debris_dynamics().and_then(|sample| validate(sample, snapshot)) {
            Ok(sample) => self.sample = Some(sample),
            Err(error) => self.error = Some(error),
        }
    }
}

fn validate(
    sample: DebrisDynamicsReport,
    snapshot: &Snapshot,
) -> Result<DebrisDynamicsReport, String> {
    if sample.source_time_s != snapshot.time_s
        || sample.frame != snapshot.frame
        || sample.moving_count > sample.physical_count
        || !sample
            .mean_velocity_world_mps
            .iter()
            .all(|value| value.is_finite())
        || !sample.total_mass_kg.is_finite()
        || sample.total_mass_kg < 0.0
        || [
            sample.min_speed_mps,
            sample.max_speed_mps,
            sample.mean_spin_rad_s,
        ]
        .into_iter()
        .flatten()
        .any(|value| !value.is_finite() || value < 0.0)
    {
        return Err("Live asteroid-body sample is unavailable or does not match this world. Retry after the next sample.".into());
    }
    Ok(sample)
}
