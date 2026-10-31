//! Native acquisition, controlled delivery and opt-in autonomous-command expiry.
use super::*;
use crate::model::{
    CommandWatchdog, CommandWatchdogStatus, ControlAuthority, ControlAuthorityStatus, ControlClaim,
    ControlFence, ControlRelease, ControlRenewal, ControlRenewalReceipt, LeaseError, LeasedCommand,
    NavigationSample, RadarFrame, RadarRow, SensorDelivery, SensorFault, SensorFaultMode,
    SensorFaultTarget, SensorState, SensorUnitId, sensor_delivery, valid_control_id,
};

/// Live runtime clock only. Neither value is loaded from or written to an archive.
#[derive(Clone, Copy, Default)]
pub(super) struct ControlRuntimeClock {
    now_s: Option<f64>,
    deadline_s: Option<f64>,
    command_deadline_s: Option<f64>,
    ownership_renewed: bool,
}

impl Simulation {
    fn sensor_state_mut(&mut self) -> &mut SensorState {
        if self.state.sensor_state.is_none() {
            let mut state = SensorState::new(self.state.seed);
            // Wrap an old archive's existing primary sample without resampling.
            // It is never offered as an independent backup measurement.
            if let Some(observation) = self.state.observations.first() {
                state.primary.acquisition_sequence = 1;
                state.primary.sample = Some(RadarFrame::unversioned(
                    observation.measurement_time_s,
                    1,
                    self.state
                        .observations
                        .iter()
                        .map(public_radar_row)
                        .collect(),
                ));
            }
            self.state.sensor_state = Some(state);
        }
        self.state
            .sensor_state
            .as_mut()
            .expect("initialized sensor extension")
    }

    pub(crate) fn sensor_revision(&self) -> u32 {
        self.state
            .sensor_state
            .as_ref()
            .map_or(0, |state| state.revision)
    }

    pub(crate) fn sensor_delivery_state(&self) -> Option<&SensorState> {
        self.state.sensor_state.as_ref()
    }

    pub fn control_authority(&self) -> ControlAuthority {
        self.stored_control_authority().cloned().unwrap_or_default()
    }

    fn stored_control_authority(&self) -> Option<&ControlAuthority> {
        self.state.sensor_state.as_ref()?.control_authority.as_ref()
    }

    pub(super) fn install_control_authority(&mut self, authority: Option<ControlAuthority>) {
        if let Some(authority) = authority {
            if authority.status != ControlAuthorityStatus::Active {
                self.control_runtime.deadline_s = None;
                self.control_runtime.ownership_renewed = false;
            }
            self.sensor_state_mut().control_authority = Some(authority);
        }
    }

    pub(super) fn retain_control_runtime_from(&mut self, previous: &Self) {
        self.control_runtime = ControlRuntimeClock {
            now_s: previous.control_runtime.now_s,
            deadline_s: None,
            command_deadline_s: None,
            ownership_renewed: false,
        };
    }

    /// Read the real engine clock even when playback is paused. Direct physics
    /// apps without Time<Real> retain their original simulation-clock fallback.
    pub(crate) fn update_control_runtime(
        &mut self,
        time: &bevy::time::Time<bevy::time::Real>,
    ) -> bool {
        let now = time.elapsed_secs_f64();
        let previous = self.control_runtime.now_s.replace(now);
        let active_expiry = self
            .stored_control_authority()
            .filter(|authority| authority.status == ControlAuthorityStatus::Active)
            .map(|authority| authority.expires_at_sim_time_s);
        if let Some(sim_expiry) = active_expiry {
            if previous.is_some_and(|previous| now < previous) {
                // A replacement clock cannot extend an existing leader's lease.
                self.control_runtime.deadline_s = Some(now);
            } else if self.control_runtime.deadline_s.is_none() {
                // A claim may precede the first runtime frame. Count that frame's
                // real elapsed interval rather than granting a fresh lease at its end.
                let start = previous.unwrap_or_else(|| (now - time.delta_secs_f64()).max(0.0));
                let remaining = sim_expiry.map_or(0.0, |expiry| expiry - self.state.time_s);
                let deadline = start + remaining;
                self.control_runtime.deadline_s =
                    Some(if deadline.is_finite() { deadline } else { now });
            }
        }
        let command_expiry = self.state.sensor_state.as_ref().and_then(|state| {
            (state.command_watchdog.status == CommandWatchdogStatus::Active
                && state.command_watchdog.controller_fence.is_some())
            .then_some(state.command_watchdog.expires_at_sim_time_s)
            .flatten()
        });
        if let Some(expiry) = command_expiry {
            if previous.is_some_and(|previous| now < previous) {
                self.control_runtime.command_deadline_s = Some(now);
            } else if self.control_runtime.command_deadline_s.is_none() {
                let start = previous.unwrap_or_else(|| (now - time.delta_secs_f64()).max(0.0));
                let deadline = start + expiry - self.state.time_s;
                self.control_runtime.command_deadline_s =
                    Some(if deadline.is_finite() { deadline } else { now });
            }
        }
        let authority_expired = self.expire_control_authority();
        self.expire_command_lease() || authority_expired
    }

    fn control_deadline_elapsed(&self, authority: &ControlAuthority) -> bool {
        authority
            .expires_at_sim_time_s
            .is_none_or(|expiry| expiry <= self.state.time_s)
            || self
                .control_runtime
                .now_s
                .zip(self.control_runtime.deadline_s)
                .is_some_and(|(now, deadline)| deadline <= now)
    }

    fn command_runtime_deadline(&self, sim_expiry: f64) -> Result<Option<f64>, LeaseError> {
        let Some(now) = self.control_runtime.now_s else {
            return Ok(None);
        };
        let deadline = now + (sim_expiry - self.state.time_s);
        if !deadline.is_finite() || deadline <= now {
            return Err(LeaseError::Expired);
        }
        Ok(Some(deadline))
    }

    fn expire_control_authority(&mut self) -> bool {
        let Some(mut authority) = self.stored_control_authority().cloned() else {
            return false;
        };
        if authority.status != ControlAuthorityStatus::Active
            || !self.control_deadline_elapsed(&authority)
        {
            return false;
        }
        self.clear_cohort_lease(&authority, CommandWatchdogStatus::Expired);
        authority.status = ControlAuthorityStatus::Expired;
        self.install_control_authority(Some(authority));
        true
    }

    pub(super) fn revoked_control_authority(&self) -> Result<Option<ControlAuthority>, String> {
        let Some(mut authority) = self.stored_control_authority().cloned() else {
            return Ok(None);
        };
        if matches!(
            authority.status,
            ControlAuthorityStatus::Active | ControlAuthorityStatus::Expired
        ) {
            authority.epoch = authority
                .epoch
                .checked_add(1)
                .ok_or("Controller consent epoch exhausted; no takeover was applied.")?;
            authority.status = ControlAuthorityStatus::Revoked;
            authority.controller_id = None;
            authority.expires_at_sim_time_s = None;
        }
        Ok(Some(authority))
    }

    /// Playback and native input revoke consent, not body velocity or legacy force.
    pub(crate) fn revoke_control(&mut self) -> Result<bool, String> {
        let replacement = self.revoked_control_authority()?;
        let Some(previous) = self.stored_control_authority().cloned() else {
            return Ok(false);
        };
        if replacement.as_ref() == Some(&previous) {
            return Ok(false);
        }
        self.clear_cohort_lease(&previous, CommandWatchdogStatus::Revoked);
        self.install_control_authority(replacement);
        Ok(true)
    }

    pub(super) fn authority_after_restore(
        &self,
        archive: &Snapshot,
    ) -> Result<Option<ControlAuthority>, String> {
        let archived = archive
            .sensor_state
            .as_ref()
            .and_then(|state| state.control_authority.as_ref());
        let current = self.stored_control_authority();
        let Some(source) = current.or(archived) else {
            return Ok(None);
        };
        let epoch = current
            .map_or(0, |authority| authority.epoch)
            .max(archived.map_or(0, |authority| authority.epoch))
            .checked_add(1)
            .ok_or("Controller consent epoch exhausted; no restore was applied.")?;
        Ok(Some(ControlAuthority {
            epoch,
            term: current
                .map_or(0, |authority| authority.term)
                .max(archived.map_or(0, |authority| authority.term)),
            status: ControlAuthorityStatus::Revoked,
            run_id: source.run_id.clone(),
            controller_id: None,
            expires_at_sim_time_s: None,
        }))
    }

    pub fn claim_control(&mut self, request: ControlClaim) -> Result<(), LeaseError> {
        if !valid_control_id(&request.run_id) || !valid_control_id(&request.controller_id) {
            return Err(LeaseError::Rejected(
                "run_id and controller_id must match [A-Za-z0-9_-]{1,64}.".into(),
            ));
        }
        let expiry = self.command_deadline(request.source_time_s, request.lease_sim_s)?;
        let runtime_expiry = self.command_runtime_deadline(expiry)?;
        let current = self.control_authority();
        if request.expected_epoch != current.epoch || request.expected_term != current.term {
            return Err(LeaseError::StaleContext);
        }
        let same_run = current.run_id.as_deref() == Some(request.run_id.as_str());
        match current.status {
            ControlAuthorityStatus::Active if !self.control_deadline_elapsed(&current) => {
                return Err(LeaseError::StaleContext);
            }
            ControlAuthorityStatus::Revoked if same_run => return Err(LeaseError::StaleContext),
            ControlAuthorityStatus::Idle
            | ControlAuthorityStatus::Active
            | ControlAuthorityStatus::Expired
            | ControlAuthorityStatus::Revoked => {}
        }
        let term = current.term.checked_add(1).ok_or_else(|| {
            LeaseError::Rejected("Controller term exhausted; no claim was applied.".into())
        })?;
        let epoch = if current.run_id.is_some()
            && !same_run
            && current.status != ControlAuthorityStatus::Revoked
        {
            current.epoch.checked_add(1).ok_or_else(|| {
                LeaseError::Rejected(
                    "Controller consent epoch exhausted; no claim was applied.".into(),
                )
            })?
        } else {
            current.epoch
        };
        // Every admitted cohort must retain capacity for manual or explicit release.
        if epoch.checked_add(1).is_none() {
            return Err(LeaseError::Rejected(
                "Controller consent epoch exhausted; no claim was applied.".into(),
            ));
        }
        self.clear_owned_lease(CommandWatchdogStatus::Expired);
        self.install_control_authority(Some(ControlAuthority {
            epoch,
            term,
            status: ControlAuthorityStatus::Active,
            run_id: Some(request.run_id),
            controller_id: Some(request.controller_id),
            expires_at_sim_time_s: Some(expiry),
        }));
        self.control_runtime.deadline_s = runtime_expiry;
        self.control_runtime.ownership_renewed = false;
        Ok(())
    }

    pub fn release_control(&mut self, request: ControlRelease) -> Result<(), LeaseError> {
        if !valid_control_id(&request.run_id) {
            return Err(LeaseError::Rejected(
                "run_id must match [A-Za-z0-9_-]{1,64}.".into(),
            ));
        }
        let current = self.control_authority();
        if request.expected_epoch != current.epoch
            || current.run_id.as_deref() != Some(request.run_id.as_str())
            || !matches!(
                current.status,
                ControlAuthorityStatus::Active | ControlAuthorityStatus::Expired
            )
        {
            return Err(LeaseError::StaleContext);
        }
        self.revoke_control().map_err(LeaseError::Rejected)?;
        Ok(())
    }

    /// Keep this live owner only. Never claim a term or touch an actuator lease.
    pub fn renew_control(
        &mut self,
        request: ControlRenewal,
    ) -> Result<ControlRenewalReceipt, LeaseError> {
        if !request.lease_sim_s.is_finite() || !(0.05..=0.75).contains(&request.lease_sim_s) {
            return Err(LeaseError::Rejected(
                "Ownership renewal lease_sim_s must be between 0.05 and 0.75.".into(),
            ));
        }
        let expiry = self.command_deadline(request.source_time_s, request.lease_sim_s)?;
        let runtime_expiry = self.command_runtime_deadline(expiry)?;
        let mut authority = self.control_authority();
        if !valid_control_id(&request.controller_fence.run_id)
            || !valid_control_id(&request.controller_fence.controller_id)
            || authority.status != ControlAuthorityStatus::Active
            || authority.fence().as_ref() != Some(&request.controller_fence)
            || self.control_deadline_elapsed(&authority)
            || authority
                .expires_at_sim_time_s
                .is_none_or(|old| expiry <= old)
        {
            return Err(LeaseError::StaleContext);
        }
        if !self.autonomous_delivery_ready() {
            return Err(LeaseError::Unavailable);
        }
        // Extending a lease on a held simulation source must not slide its real
        // runtime origin. Only the source-based deadline increase is available.
        let runtime_expiry = match (runtime_expiry, self.control_runtime.deadline_s) {
            (Some(proposed), Some(previous)) => {
                Some(proposed.min(previous + (expiry - authority.expires_at_sim_time_s.unwrap())))
            }
            (proposed, _) => proposed,
        };
        authority.expires_at_sim_time_s = Some(expiry);
        self.install_control_authority(Some(authority.clone()));
        self.control_runtime.deadline_s = runtime_expiry;
        self.control_runtime.ownership_renewed = true;
        Ok(ControlRenewalReceipt {
            source_time_s: request.source_time_s,
            lease_sim_s: request.lease_sim_s,
            sensor_revision: self.sensor_revision(),
            control_authority: authority,
        })
    }

    /// Checked Play may start this cohort but must not authorize a revoked one.
    pub(crate) fn control_fence_for_play(&self) -> Result<Option<ControlFence>, LeaseError> {
        let Some(authority) = self.stored_control_authority() else {
            return Ok(None);
        };
        if authority.status != ControlAuthorityStatus::Active
            || self.control_deadline_elapsed(authority)
        {
            return Err(LeaseError::StaleContext);
        }
        authority.fence().map(Some).ok_or(LeaseError::StaleContext)
    }

    fn command_deadline(&self, source_time_s: f64, lease_sim_s: f64) -> Result<f64, LeaseError> {
        if !lease_sim_s.is_finite() || !(0.05..=1.0).contains(&lease_sim_s) {
            return Err(LeaseError::Rejected(
                "lease_sim_s must be finite and between 0.05 and 1.0.".into(),
            ));
        }
        if !source_time_s.is_finite() || source_time_s < 0.0 || source_time_s > self.state.time_s {
            return Err(LeaseError::Rejected(
                "source_time_s must be a finite, nonfuture simulation timestamp.".into(),
            ));
        }
        let expiry = source_time_s + lease_sim_s;
        if !expiry.is_finite() || expiry <= self.state.time_s {
            return Err(LeaseError::Expired);
        }
        Ok(expiry)
    }

    pub fn set_sensor_fault(
        &mut self,
        target: SensorFaultTarget,
        mode: SensorFaultMode,
        duration_sim_s: Option<f64>,
    ) -> Result<(), String> {
        if duration_sim_s
            .is_some_and(|duration| !duration.is_finite() || !(0.05..=300.0).contains(&duration))
            || (mode != SensorFaultMode::Nominal && duration_sim_s.is_none())
        {
            return Err(
                "Non-nominal sensor faults require finite duration_sim_s between 0.05 and 300."
                    .into(),
            );
        }
        let mut faults = self
            .state
            .sensor_state
            .as_ref()
            .map_or_else(Vec::new, |state| state.faults.clone());
        faults.retain(|fault| fault.target != target);
        if mode != SensorFaultMode::Nominal {
            let expiry = self.state.time_s + duration_sim_s.expect("validated duration");
            if !expiry.is_finite() || expiry <= self.state.time_s {
                return Err(
                    "Sensor fault duration cannot be represented at this simulation time.".into(),
                );
            }
            faults.push(SensorFault {
                target,
                mode,
                expires_at_sim_time_s: expiry,
            });
        }
        self.replace_sensor_faults(faults)
    }

    pub fn clear_sensor_faults(&mut self) -> Result<(), String> {
        self.replace_sensor_faults(Vec::new())
    }

    fn replace_sensor_faults(&mut self, faults: Vec<SensorFault>) -> Result<(), String> {
        let previous = self.state.sensor_state.as_ref();
        if previous.is_none_or(|state| state.faults == faults) && faults.is_empty() {
            return Ok(());
        }
        if previous.is_some_and(|state| state.faults == faults) {
            return Ok(());
        }
        let revision = self.sensor_revision();
        // Reserve a revision for each possible future automatic expiry.
        if revision.checked_add(1 + faults.len() as u32).is_none() {
            return Err(
                "Sensor revision exhausted; configure a new trial before changing faults.".into(),
            );
        }
        let before = SensorUnitId::ALL
            .map(|unit| previous.map_or(SensorFaultMode::Nominal, |state| state.mode(unit)));
        let state = self.sensor_state_mut();
        state.faults = faults;
        state.revision = revision + 1;
        Self::mark_sensor_recovery(state, before);
        self.clear_owned_lease(CommandWatchdogStatus::SensorChanged);
        Ok(())
    }

    fn mark_sensor_recovery(state: &mut SensorState, before: [SensorFaultMode; 5]) {
        for (unit, old_mode) in SensorUnitId::ALL.into_iter().zip(before) {
            if old_mode == SensorFaultMode::Nominal || state.mode(unit) != SensorFaultMode::Nominal
            {
                continue;
            }
            match unit {
                SensorUnitId::RadarPrimary => state.primary.waiting_for_sample = true,
                SensorUnitId::RadarBackup => state.backup.waiting_for_sample = true,
                SensorUnitId::Navigation => state.navigation.waiting_for_sample = true,
                SensorUnitId::CameraTop | SensorUnitId::CameraBottom => {}
            }
        }
    }

    fn expire_sensor_faults(&mut self) {
        let now = self.state.time_s;
        let Some(state) = self.state.sensor_state.as_mut() else {
            return;
        };
        if !state
            .faults
            .iter()
            .any(|fault| fault.expires_at_sim_time_s <= now)
        {
            return;
        }
        let before = SensorUnitId::ALL.map(|unit| state.mode(unit));
        state
            .faults
            .retain(|fault| fault.expires_at_sim_time_s > now);
        state.revision = state
            .revision
            .checked_add(1)
            .expect("fault edits reserve expiry revisions");
        Self::mark_sensor_recovery(state, before);
        self.clear_owned_lease(CommandWatchdogStatus::SensorChanged);
    }

    pub fn set_leased_command(&mut self, request: LeasedCommand) -> Result<(), LeaseError> {
        if !request
            .accel
            .iter()
            .chain(&request.angular_accel)
            .all(|value| value.is_finite())
        {
            return Err(LeaseError::Rejected("command values must be finite".into()));
        }
        let expiry = self.command_deadline(request.source_time_s, request.lease_sim_s)?;
        match (&request.controller_fence, self.stored_control_authority()) {
            (None, None) => {}
            (Some(fence), Some(authority))
                if valid_control_id(&fence.run_id)
                    && valid_control_id(&fence.controller_id)
                    && authority.status == ControlAuthorityStatus::Active
                    && authority.fence().as_ref() == Some(fence)
                    && !self.control_deadline_elapsed(authority) => {}
            _ => return Err(LeaseError::StaleContext),
        }
        let nonzero = request
            .accel
            .iter()
            .chain(&request.angular_accel)
            .any(|value| *value != 0.0);
        if nonzero && !self.autonomous_delivery_ready() {
            return Err(LeaseError::Unavailable);
        }
        let command_runtime_expiry = if request.controller_fence.is_some() {
            self.command_runtime_deadline(expiry)?
        } else {
            None
        };
        let authority_expiry = request.controller_fence.as_ref().map(|_| {
            if self.control_runtime.ownership_renewed {
                self.control_authority()
                    .expires_at_sim_time_s
                    .unwrap()
                    .max(expiry)
            } else {
                // Cohorts that never opted into ownership-only renewal retain
                // the existing direct atomic actuator-renewal contract.
                expiry
            }
        });
        let authority_runtime_expiry = match authority_expiry {
            Some(owner_expiry)
                if !self.control_runtime.ownership_renewed
                    || expiry >= self.control_authority().expires_at_sim_time_s.unwrap() =>
            {
                self.command_runtime_deadline(owner_expiry)?
            }
            _ => self.control_runtime.deadline_s,
        };
        // Do not route fenced writes through manual SetCommand's consent takeover.
        self.set_bounded_command(request.accel, request.angular_accel)
            .map_err(LeaseError::Rejected)?;
        if let Some(authority_expiry) = authority_expiry {
            self.sensor_state_mut()
                .control_authority
                .as_mut()
                .expect("validated live controller authority")
                .expires_at_sim_time_s = Some(authority_expiry);
            self.control_runtime.deadline_s = authority_runtime_expiry;
        }
        self.control_runtime.command_deadline_s = command_runtime_expiry;
        self.sensor_state_mut().command_watchdog = CommandWatchdog {
            status: CommandWatchdogStatus::Active,
            source_time_s: Some(request.source_time_s),
            expires_at_sim_time_s: Some(expiry),
            controller_fence: request.controller_fence,
        };
        Ok(())
    }

    fn autonomous_delivery_ready(&self) -> bool {
        let Some(state) = &self.state.sensor_state else {
            // Legacy raw commands remain available, but autonomous leases need
            // the new delivery metadata from a genuine acquisition first.
            return false;
        };
        if state.navigation_delivery() != SensorDelivery::Ready {
            return false;
        }
        [
            (SensorUnitId::RadarPrimary, &state.primary),
            (SensorUnitId::RadarBackup, &state.backup),
        ]
        .into_iter()
        .any(|(unit, radar)| {
            sensor_delivery(
                state.mode(unit),
                radar.sample.is_some(),
                radar.waiting_for_sample,
            ) == SensorDelivery::Ready
        })
    }

    pub(super) fn cancel_command_lease(&mut self) {
        self.control_runtime.command_deadline_s = None;
        if let Some(state) = &mut self.state.sensor_state {
            state.command_watchdog = CommandWatchdog::default();
        }
    }

    fn clear_cohort_lease(&mut self, authority: &ControlAuthority, reason: CommandWatchdogStatus) {
        let owned = self.state.sensor_state.as_ref().is_some_and(|state| {
            state
                .command_watchdog
                .controller_fence
                .as_ref()
                .is_some_and(|fence| {
                    fence.epoch == authority.epoch
                        && Some(&fence.run_id) == authority.run_id.as_ref()
                })
        });
        if owned {
            self.clear_owned_lease(reason);
        }
    }

    fn clear_owned_lease(&mut self, reason: CommandWatchdogStatus) {
        let Some(watchdog) = self
            .state
            .sensor_state
            .as_ref()
            .map(|state| state.command_watchdog.clone())
        else {
            return;
        };
        if watchdog.status != CommandWatchdogStatus::Active {
            return;
        }
        self.control_runtime.command_deadline_s = None;
        // A successful manual/legacy replacement already removed this marker.
        // Zero forces, not velocity. The next normal step still applies gravity.
        self.set_bounded_command([0.0; 3], [0.0; 3])
            .expect("validated world has its controlled lander");
        if let Some(body) = self
            .handles
            .get(&EntityId::Lander)
            .and_then(|handle| self.bodies.get_mut(*handle))
        {
            body.reset_forces(false);
            body.reset_torques(false);
        }
        self.last_actuator_application = None;
        self.sensor_state_mut().command_watchdog = CommandWatchdog {
            status: reason,
            ..watchdog
        };
    }

    pub(super) fn restore_command_lease(&mut self) {
        self.clear_owned_lease(CommandWatchdogStatus::Restored);
        let authority = self
            .revoked_control_authority()
            .expect("archived authority must retain revocation capacity");
        self.install_control_authority(authority);
    }

    pub(super) fn enforce_sensor_and_command_deadlines(&mut self) {
        self.expire_sensor_faults();
        self.expire_control_authority();
        self.expire_command_lease();
    }

    fn expire_command_lease(&mut self) -> bool {
        let expired = self.state.sensor_state.as_ref().is_some_and(|state| {
            state.command_watchdog.status == CommandWatchdogStatus::Active
                && (state
                    .command_watchdog
                    .expires_at_sim_time_s
                    .is_some_and(|expiry| expiry <= self.state.time_s)
                    || self
                        .control_runtime
                        .now_s
                        .zip(self.control_runtime.command_deadline_s)
                        .is_some_and(|(now, deadline)| deadline <= now))
        });
        if expired {
            self.clear_owned_lease(CommandWatchdogStatus::Expired);
        }
        expired
    }

    pub(super) fn invalidate_sensor_samples(&mut self) {
        self.cancel_command_lease();
        if let Some(state) = &mut self.state.sensor_state {
            state.primary.sample = None;
            state.primary.waiting_for_sample = true;
            state.backup.sample = None;
            state.backup.waiting_for_sample = true;
            state.navigation.sample = None;
            state.navigation.waiting_for_sample =
                state.mode(SensorUnitId::Navigation) != SensorFaultMode::Nominal;
        }
    }

    fn live_navigation_sample(&self, sequence: u64) -> Option<NavigationSample> {
        let shuttle = self
            .state
            .assets
            .iter()
            .find(|entity| entity.id == EntityId::Lander)?
            .clone();
        let target = self
            .state
            .assets
            .iter()
            .find(|entity| entity.id == EntityId::ReturnShip)?
            .clone();
        let terrain_radius_at_shuttle_m = bevy::math::DVec3::from_array(shuttle.position)
            .try_normalize()
            .map(crate::terrain::surface_radius_m);
        let (applied_physics, applied_physics_error) = match self.applied_physics() {
            Ok(physics) => (Some(physics), None),
            Err(error) => (None, Some(error)),
        };
        Some(NavigationSample {
            sample_time_s: self.state.time_s,
            sequence,
            shuttle,
            target,
            mission: self.state.mission.clone(),
            applied_physics,
            applied_physics_error,
            terrain_radius_at_shuttle_m,
        })
    }

    pub(super) fn sample_sensor_deliveries(&mut self) {
        let mut backup_rng = self.sensor_state_mut().backup_rng_state;
        let backup_rows = sample_backup(&self.state, &mut backup_rng);
        let primary_rows = self
            .state
            .observations
            .iter()
            .map(public_radar_row)
            .collect();
        let distribution = self
            .state
            .experiment_settings
            .map(|experiment| experiment.debris.distribution);
        let now = self.state.time_s;
        let state = self.sensor_state_mut();
        state.backup_rng_state = backup_rng;
        state.primary.acquisition_sequence = state.primary.acquisition_sequence.saturating_add(1);
        state.backup.acquisition_sequence = state.backup.acquisition_sequence.saturating_add(1);
        if state.mode(SensorUnitId::RadarPrimary) == SensorFaultMode::Nominal {
            state.primary.sample = Some(RadarFrame::acquired(
                now,
                state.primary.acquisition_sequence,
                primary_rows,
                distribution,
            ));
            state.primary.waiting_for_sample = false;
        }
        if state.mode(SensorUnitId::RadarBackup) == SensorFaultMode::Nominal {
            state.backup.sample = Some(RadarFrame::acquired(
                now,
                state.backup.acquisition_sequence,
                backup_rows,
                distribution,
            ));
            state.backup.waiting_for_sample = false;
        }
        self.sample_navigation_delivery();
    }

    pub(super) fn sample_navigation_delivery(&mut self) {
        // Navigation is the existing direct-state feed, not radar-cadence data.
        // Its sequence must advance with the actual physics timestamp it reports.
        let now = self.state.time_s;
        let state = self.sensor_state_mut();
        if state.mode(SensorUnitId::Navigation) != SensorFaultMode::Nominal
            || state
                .navigation
                .sample
                .as_ref()
                .is_some_and(|sample| sample.sample_time_s == now)
        {
            return;
        }
        let sequence = state.navigation.acquisition_sequence.saturating_add(1);
        let sample = self.live_navigation_sample(sequence);
        let state = self.sensor_state_mut();
        state.navigation.acquisition_sequence = sequence;
        state.navigation.sample = sample;
        state.navigation.waiting_for_sample = false;
    }

    pub(super) fn project_sensor_delivery(&self, report: &mut Value) {
        report["control_capabilities"] = json!({
            "ownership_renewal": {"schema_version": 1, "max_lease_sim_s": 0.75}
        });
        report["control_authority"] = json!(self.control_authority());
        report["radar_backup"] = json!([]);
        report["navigation_sample_time_s"] = json!(self.state.time_s);
        report["command_watchdog"] = json!(CommandWatchdog::default());
        let mut health = Vec::new();
        for unit in SensorUnitId::ALL {
            let state = self.state.sensor_state.as_ref();
            let mode = state.map_or(SensorFaultMode::Nominal, |state| state.mode(unit));
            let mut radar_provenance = "unknown";
            let (sample_time, sequence, has_sample, waiting) = match unit {
                SensorUnitId::RadarPrimary | SensorUnitId::RadarBackup => {
                    let radar = state.and_then(|state| match unit {
                        SensorUnitId::RadarPrimary => Some(&state.primary),
                        SensorUnitId::RadarBackup => Some(&state.backup),
                        SensorUnitId::CameraTop
                        | SensorUnitId::CameraBottom
                        | SensorUnitId::Navigation => None,
                    });
                    let sample = radar.and_then(|radar| radar.sample.as_ref());
                    if let Some(radar) = radar {
                        let available = mode == SensorFaultMode::Frozen
                            || (mode == SensorFaultMode::Nominal && !radar.waiting_for_sample);
                        let delivered = sample.filter(|_| available);
                        let rows = delivered.map_or(&[][..], |sample| sample.rows.as_slice());
                        // Rows and provenance belong to the same cached frame,
                        // including nominal delivery after an archive restore.
                        if unit == SensorUnitId::RadarPrimary {
                            report["radar"] = json!(rows);
                            if let Some(sample) = delivered {
                                report["radar_selection"]["selection"] =
                                    json!(sample.selection_label());
                                radar_provenance = sample.primary_provenance();
                            }
                        } else {
                            report["radar_backup"] = json!(rows);
                            if let Some(sample) = delivered {
                                radar_provenance = sample.backup_provenance();
                            }
                        }
                    }
                    (
                        sample.map(|sample| sample.sample_time_s),
                        sample.map(|sample| sample.sequence),
                        sample.is_some(),
                        radar.is_some_and(|radar| radar.waiting_for_sample),
                    )
                }
                SensorUnitId::Navigation => {
                    let navigation = state.map(|state| &state.navigation);
                    let sample = navigation.and_then(|navigation| navigation.sample.as_ref());
                    let waiting =
                        navigation.is_some_and(|navigation| navigation.waiting_for_sample);
                    if mode == SensorFaultMode::Frozen {
                        if let Some(sample) = sample {
                            report["shuttle"] = navigation_shuttle_report(
                                &sample.shuttle,
                                sample.applied_physics.as_ref(),
                            );
                            report["target"] = json!(sample.target);
                            report["mission"] = json!(sample.mission);
                            // The mission stop signal is control-plane state,
                            // not a navigation sample. Frozen telemetry must not
                            // conceal a genuine terminal collision or arrival.
                            report["mission"]["status"] = json!(self.state.mission.status);
                            if self.state.mission.status.is_terminal() {
                                report["mission"]["phase"] = json!(self.state.mission.phase);
                            }
                            report["applied_physics"] = json!(sample.applied_physics);
                            report["applied_physics_error"] = json!(sample.applied_physics_error);
                            report["geometry"]["terrain_radius_at_shuttle_m"] =
                                json!(sample.terrain_radius_at_shuttle_m);
                        } else {
                            hide_navigation(report);
                        }
                    } else if mode == SensorFaultMode::Unavailable || waiting {
                        hide_navigation(report);
                    }
                    let live = mode == SensorFaultMode::Nominal && !waiting && sample.is_some();
                    let timestamp = if live {
                        Some(self.state.time_s)
                    } else if mode == SensorFaultMode::Frozen {
                        sample.map(|sample| sample.sample_time_s)
                    } else {
                        None
                    };
                    report["navigation_sample_time_s"] = json!(timestamp);
                    (
                        timestamp,
                        sample.map(|sample| sample.sequence),
                        live || sample.is_some(),
                        waiting,
                    )
                }
                SensorUnitId::CameraTop | SensorUnitId::CameraBottom => (None, None, false, false),
            };
            let delivery = sensor_delivery(mode, has_sample, waiting);
            let radar = matches!(unit, SensorUnitId::RadarPrimary | SensorUnitId::RadarBackup);
            let cfg = settings();
            let shared = if radar {
                vec![SensorFaultTarget::RadarBus, SensorFaultTarget::AllAvionics]
            } else {
                vec![SensorFaultTarget::AllAvionics]
            };
            health.push(json!({
                "id": unit,
                "delivery": delivery,
                "active_faults": state.map_or_else(Vec::new, |state| state.active_faults(unit)),
                "sample_time_s": if matches!(delivery, SensorDelivery::Ready | SensorDelivery::Frozen) { sample_time } else { None },
                "sequence": if matches!(delivery, SensorDelivery::Ready | SensorDelivery::Frozen) { sequence } else { None },
                "provenance": match unit {
                    SensorUnitId::RadarPrimary | SensorUnitId::RadarBackup => radar_provenance,
                    SensorUnitId::CameraTop | SensorUnitId::CameraBottom => "native_gpu_rgb_capture",
                    SensorUnitId::Navigation => "native_unreplicated_navigation_readback_no_noise_model",
                },
                "noise": {
                    "range_noise_m": radar.then_some(cfg.sensors.range_noise_m),
                    "range_rate_noise_mps": radar.then_some(cfg.sensors.range_rate_noise_mps),
                    "diameter_noise_fraction": radar.then_some(cfg.debris.diameter_noise_fraction),
                },
                "shared_fault_domains": shared,
            }));
        }
        report["sensor_health"] =
            json!({"schema_version": 1, "revision": self.sensor_revision(), "units": health});
        if let Some(state) = &self.state.sensor_state {
            report["command_watchdog"] = json!(state.command_watchdog);
        }
    }
}

fn public_radar_row(observation: &Observation) -> RadarRow {
    [
        observation.relative_position_m,
        [
            observation.range_m,
            observation.range_rate_mps,
            observation.diameter_m,
        ],
        [observation.measurement_time_s, 0.0, 0.0],
    ]
}

fn sample_backup(state: &Snapshot, rng: &mut u64) -> Vec<RadarRow> {
    let Some(shuttle) = state
        .assets
        .iter()
        .find(|entity| entity.id == EntityId::Lander)
    else {
        return Vec::new();
    };
    let cfg = settings();
    let mut rows = Vec::with_capacity(cfg.debris.radar_count);
    let candidate_limit = radar_candidate_limit(state);
    for debris in state
        .assets
        .iter()
        .filter(|entity| entity.kind == EntityKind::Debris)
        .take(candidate_limit)
    {
        let delta = sub(debris.position, shuttle.position);
        let range = norm(delta);
        let relative_velocity = sub(debris.velocity, shuttle.velocity);
        let position =
            std::array::from_fn(|axis| delta[axis] + gaussian(rng) * cfg.sensors.range_noise_m);
        let geometric_rate = if range > 0.0 {
            dot(relative_velocity, delta) / range
        } else {
            0.0
        };
        let rate = geometric_rate + gaussian(rng) * cfg.sensors.range_rate_noise_mps;
        let closest = range - debris.radius_m + gaussian(rng) * cfg.sensors.range_noise_m;
        let diameter =
            debris.radius_m * 2.0 * (1.0 + gaussian(rng) * cfg.debris.diameter_noise_fraction);
        rows.push([
            position,
            [closest, rate, diameter],
            [state.time_s, 0.0, 0.0],
        ]);
    }
    rows.sort_by(|a, b| norm(a[0]).total_cmp(&norm(b[0])));
    rows.truncate(cfg.debris.radar_count);
    rows
}

fn hide_navigation(report: &mut Value) {
    for field in ["shuttle", "target", "applied_physics"] {
        report[field] = Value::Null;
    }
    report["applied_physics_error"] =
        json!("Navigation sensor delivery unavailable; no live body readback is exposed.");
    report["geometry"]["terrain_radius_at_shuttle_m"] = Value::Null;
    // Keep mission outcome available for stopping, not exact live relative state.
    for field in [
        "target_distance_m",
        "relative_speed_mps",
        "fuel_used_kg",
        "damage",
        "score",
    ] {
        report["mission"][field] = Value::Null;
    }
}

fn navigation_shuttle_report(
    ship: &EntityState,
    applied: Option<&crate::model::AppliedPhysics>,
) -> Value {
    let mut report = json!(ship);
    report["wrench_provenance"] = json!("command_derived_local_calibration");
    if let Some(actual) = applied {
        let inertia = actual.inertia_body_kg_m2;
        report["inertia_kg_m2"] = json!(inertia);
        report["inertia_frame"] = json!("body");
        report["mass_kg"] = json!(actual.mass_kg);
        report["actuation_state"] = json!(actual.actuation_state);
        let application = actual.last_actuator_application;
        report["actuator_application_time_s"] =
            json!(application.map(|applied| applied.application_time_s));
        report["applied_accel_mps2"] =
            json!(application.map(|applied| scale(applied.force_body_n, 1.0 / actual.mass_kg)));
        report["applied_angular_accel_rad_s2"] = json!(application.map(|applied| {
            (inertia_matrix(inertia).inverse() * Vector::from_array(applied.torque_body_nm))
                .to_array()
        }));
    }
    report
}

pub(super) fn validate_sensor_state(snapshot: &Snapshot) -> Result<(), String> {
    let Some(state) = &snapshot.sensor_state else {
        return Ok(());
    };
    if state.schema_version != 1
        || state.backup_rng_state == 0
        || state.faults.len() > 7
        || state
            .revision
            .checked_add(state.faults.len() as u32)
            .is_none()
    {
        return Err("Invalid archived sensor schema, RNG or revision capacity.".into());
    }
    let mut targets = Vec::new();
    for fault in &state.faults {
        if targets.contains(&fault.target)
            || fault.mode == SensorFaultMode::Nominal
            || !fault.expires_at_sim_time_s.is_finite()
            || fault.expires_at_sim_time_s < 0.0
            || fault.expires_at_sim_time_s > snapshot.time_s + 300.0
        {
            return Err("Invalid archived sensor fault.".into());
        }
        targets.push(fault.target);
    }
    let valid_time = |time: f64| time.is_finite() && time >= 0.0 && time <= snapshot.time_s;
    // The official cloud publishes a rolling scan: rows are acquired at
    // different 120 Hz iterations, so each row keeps its own timestamp at or
    // before the publication time. Every other distribution keeps one frame clock.
    let rolling = snapshot
        .experiment_settings
        .is_some_and(|experiment| experiment.debris.distribution.is_official_cloud());
    for radar in [&state.primary, &state.backup] {
        if let Some(sample) = &radar.sample
            && (!valid_time(sample.sample_time_s)
                || sample.sequence > radar.acquisition_sequence
                || sample.rows.len()
                    > settings()
                        .debris
                        .radar_count
                        .min(snapshot.assets.len().saturating_sub(3))
                || sample.rows.iter().any(|row| {
                    if !row.iter().flatten().all(|value| value.is_finite()) {
                        return true;
                    }
                    if rolling {
                        !(row[2][0].is_finite()
                            && row[2][0] >= 0.0
                            && row[2][0] <= sample.sample_time_s
                            && row[2][1] == 0.0
                            && row[2][2] == 0.0)
                    } else {
                        row[2] != [sample.sample_time_s, 0.0, 0.0]
                    }
                }))
        {
            return Err("Invalid archived radar delivery sample.".into());
        }
    }
    if let Some(sample) = &state.navigation.sample {
        if !valid_time(sample.sample_time_s)
            || sample.sequence > state.navigation.acquisition_sequence
            || sample.shuttle.id != EntityId::Lander
            || sample.shuttle.kind != EntityKind::Lander
            || sample.target.id != EntityId::ReturnShip
            || sample.target.kind != EntityKind::ReturnShip
            || sample.mission.shuttle_id != EntityId::Lander
            || sample.mission.target_id != EntityId::ReturnShip
            || !cached_actor_is_finite(&sample.shuttle)
            || !cached_actor_is_finite(&sample.target)
            || sample
                .terrain_radius_at_shuttle_m
                .is_some_and(|value| !value.is_finite() || value <= 0.0)
            || ![
                sample.mission.target_distance_m,
                sample.mission.initial_distance_m,
                sample.mission.relative_speed_mps,
                sample.mission.fuel_used_kg,
                sample.mission.damage,
            ]
            .into_iter()
            .all(f64::is_finite)
            || sample.mission.score.is_some_and(|value| !value.is_finite())
        {
            return Err("Invalid archived navigation delivery sample.".into());
        }
        if let Some(physics) = &sample.applied_physics
            && (!valid_time(physics.source_time_s)
                || physics.source_time_s != sample.sample_time_s
                || !cached_physics_is_finite(physics))
        {
            return Err("Invalid archived navigation physics sample.".into());
        }
    }
    let watchdog = &state.command_watchdog;
    if let Some(authority) = &state.control_authority {
        let ids_valid = authority.run_id.as_deref().is_none_or(valid_control_id)
            && authority
                .controller_id
                .as_deref()
                .is_none_or(valid_control_id);
        let valid = match authority.status {
            ControlAuthorityStatus::Idle => {
                authority.term == 0
                    && authority.run_id.is_none()
                    && authority.controller_id.is_none()
                    && authority.expires_at_sim_time_s.is_none()
            }
            ControlAuthorityStatus::Active | ControlAuthorityStatus::Expired => {
                authority.term > 0
                    && authority.run_id.is_some()
                    && authority.controller_id.is_some()
                    && authority.epoch.checked_add(1).is_some()
                    && authority.expires_at_sim_time_s.is_some_and(|expiry| {
                        expiry.is_finite()
                            && expiry >= 0.0
                            && expiry <= snapshot.time_s + 1.0
                            && match authority.status {
                                ControlAuthorityStatus::Active => expiry > snapshot.time_s,
                                // The private runtime deadline can expire while
                                // simulation time is paused before this deadline.
                                ControlAuthorityStatus::Expired => true,
                                ControlAuthorityStatus::Idle | ControlAuthorityStatus::Revoked => {
                                    false
                                }
                            }
                    })
            }
            ControlAuthorityStatus::Revoked => {
                authority.controller_id.is_none() && authority.expires_at_sim_time_s.is_none()
            }
        };
        if !ids_valid || !valid {
            return Err("Invalid archived controller authority.".into());
        }
    }
    if let Some(fence) = &watchdog.controller_fence {
        if !valid_control_id(&fence.run_id)
            || !valid_control_id(&fence.controller_id)
            || fence.term == 0
            || watchdog.status == CommandWatchdogStatus::Idle
            || state.control_authority.as_ref().is_none_or(|authority| {
                fence.epoch > authority.epoch
                    || fence.term > authority.term
                    || (watchdog.status == CommandWatchdogStatus::Active
                        && (authority.status != ControlAuthorityStatus::Active
                            || authority.fence().as_ref() != Some(fence)
                            || authority
                                .expires_at_sim_time_s
                                .zip(watchdog.expires_at_sim_time_s)
                                .is_none_or(|(owner, command)| owner < command)))
            })
        {
            return Err("Invalid archived controller command fence.".into());
        }
    } else if watchdog.status == CommandWatchdogStatus::Active && state.control_authority.is_some()
    {
        return Err("An unfenced archived lease cannot own controller authority.".into());
    }
    match (watchdog.source_time_s, watchdog.expires_at_sim_time_s) {
        (None, None) if watchdog.status == CommandWatchdogStatus::Idle => {}
        (Some(source), Some(expiry))
            if watchdog.status != CommandWatchdogStatus::Idle
                && valid_time(source)
                && expiry.is_finite()
                && (0.05 - 1e-9..=1.0 + 1e-9).contains(&(expiry - source)) => {}
        _ => return Err("Invalid archived autonomous command lease.".into()),
    }
    if !matches!(
        watchdog.status,
        CommandWatchdogStatus::Idle | CommandWatchdogStatus::Active
    ) && snapshot
        .assets
        .iter()
        .find(|entity| entity.id == EntityId::Lander)
        .is_some_and(|ship| {
            ship.commanded_accel_mps2
                .iter()
                .chain(&ship.commanded_angular_accel_rad_s2)
                .any(|value| *value != 0.0)
        })
    {
        return Err("An archived revoked autonomous lease cannot retain nonzero commands.".into());
    }
    Ok(())
}

fn cached_actor_is_finite(actor: &EntityState) -> bool {
    actor
        .position
        .iter()
        .chain(&actor.velocity)
        .chain(&actor.rotation)
        .chain(&actor.angular_velocity_rad_s)
        .chain(&actor.commanded_accel_mps2)
        .chain(&actor.commanded_angular_accel_rad_s2)
        .chain(&actor.thrust_n)
        .chain(&actor.torque_nm)
        .all(|value| value.is_finite())
        && [
            actor.radius_m,
            actor.mass_kg,
            actor.fuel_kg,
            actor.max_thrust_n,
            actor.actuator_gain,
        ]
        .into_iter()
        .all(f64::is_finite)
        && actor.mass_kg > 0.0
        && actor.radius_m > 0.0
        && (0.99..=1.01).contains(
            &actor
                .rotation
                .iter()
                .map(|value| value * value)
                .sum::<f64>()
                .sqrt(),
        )
}

fn cached_physics_is_finite(physics: &crate::model::AppliedPhysics) -> bool {
    [physics.mass_kg, physics.actuator_gain]
        .into_iter()
        .all(f64::is_finite)
        && physics.mass_kg > 0.0
        && inertia_matrix(physics.inertia_body_kg_m2)
            .inverse()
            .to_cols_array()
            .into_iter()
            .all(f64::is_finite)
        && physics
            .inertia_body_kg_m2
            .iter()
            .flatten()
            .chain(&physics.position_world_m)
            .chain(&physics.rotation_body_to_world_xyzw)
            .chain(&physics.velocity_world_mps)
            .chain(&physics.angular_velocity_world_rad_s)
            .chain(&physics.total_external_force_world_n)
            .chain(&physics.total_external_torque_world_nm)
            .chain(&physics.requested.accel_body_mps2)
            .chain(&physics.requested.angular_accel_body_rad_s2)
            .all(|value| value.is_finite())
        && physics.last_actuator_application.is_none_or(|applied| {
            [
                applied.application_time_s,
                applied.actuator_gain,
                applied.propellant_force_n,
            ]
            .into_iter()
            .all(f64::is_finite)
                && applied
                    .rotation_body_to_world_xyzw
                    .iter()
                    .chain(&applied.force_body_n)
                    .chain(&applied.torque_body_nm)
                    .chain(&applied.force_world_n)
                    .chain(&applied.torque_world_nm)
                    .chain(&applied.requested.accel_body_mps2)
                    .chain(&applied.requested.angular_accel_body_rad_s2)
                    .all(|value| value.is_finite())
        })
}

#[cfg(test)]
mod tests;
