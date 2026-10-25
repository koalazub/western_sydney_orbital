//! Typed, read-only inventory shared by the flight strip and native task panels.
use super::data::*;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub(crate) enum FlightMetric {
    Speed,
    TerrainClearance,
    Climb,
    TargetRange,
    Closing,
    ThrustAcceleration,
    Force,
    Torque,
    Spin,
    FuelUsed,
    Damage,
}

impl FlightMetric {
    pub(crate) const STRIP: [Self; 6] = [
        Self::Speed,
        Self::TerrainClearance,
        Self::Climb,
        Self::TargetRange,
        Self::Closing,
        Self::ThrustAcceleration,
    ];

    pub(crate) fn strip_label(self) -> &'static str {
        match self {
            Self::Speed => "Speed",
            Self::TerrainClearance => "Terrain · center",
            Self::Climb => "Climb · radial",
            Self::TargetRange => "Target · center",
            Self::Closing => "Closing · +toward",
            Self::ThrustAcceleration => "Thrust · last",
            Self::Force => "Last actuator force",
            Self::Torque => "Last actuator torque",
            Self::Spin => "Spin",
            Self::FuelUsed => "Fuel used · est.",
            Self::Damage => "Damage · est.",
        }
    }
}

impl FlightMetrics {
    /// Exhaustive destructuring makes adding a measured field a UI-inventory
    /// change too. No serialized field names or JSON are used by the native HUD.
    pub(super) fn inventory(self) -> [(FlightMetric, Option<f64>); 11] {
        let Self {
            speed_mps,
            radial_climb_mps,
            terrain_center_clearance_m,
            target_center_range_m,
            target_closing_mps,
            applied_thrust_accel_mps2,
            thrust_force_n,
            torque_nm,
            angular_speed_rad_s,
            fuel_used_kg_estimate,
            damage_j_estimate,
        } = self;
        [
            (FlightMetric::Speed, speed_mps),
            (FlightMetric::TerrainClearance, terrain_center_clearance_m),
            (FlightMetric::Climb, radial_climb_mps),
            (FlightMetric::TargetRange, target_center_range_m),
            (FlightMetric::Closing, target_closing_mps),
            (FlightMetric::ThrustAcceleration, applied_thrust_accel_mps2),
            (FlightMetric::Force, thrust_force_n),
            (FlightMetric::Torque, torque_nm),
            (FlightMetric::Spin, angular_speed_rad_s),
            (FlightMetric::FuelUsed, fuel_used_kg_estimate),
            (FlightMetric::Damage, damage_j_estimate),
        ]
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub(crate) enum Axis {
    X,
    Y,
    Z,
}

impl Axis {
    fn index(self) -> usize {
        match self {
            Self::X => 0,
            Self::Y => 1,
            Self::Z => 2,
        }
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub(crate) enum ActorField {
    Position,
    Velocity,
    Rotation,
    AngularVelocity,
    Mass,
    Radius,
    StoredFuel,
    RequestedAcceleration,
    RequestedAngularAcceleration,
    AppliedAcceleration,
    AppliedForce,
    AppliedTorque,
    MaximumThrust,
    ActuatorGain,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub(crate) enum Instrument {
    Flight(FlightMetric),
    ObservedVector,
    ObservedMagnitude,
    IntervalStart,
    IntervalEnd,
    IntervalDuration,
    PeakSpeed,
    PeakClimb,
    PeakClearance,
    Ship(ActorField),
    Target(ActorField),
    InertiaRow(Axis),
    BodyMass,
    ActuationStatus,
    LastApplicationTime,
    LastApplicationRevision,
    LastForce,
    LastTorque,
    TotalExternalForce,
    TotalExternalTorque,
    Collisions,
    ActiveContacts,
    ManualInterventions,
    TuningRevision,
    DebrisCount,
    StreamSpeed,
    BirthSpread,
    StreamDirection,
    DebrisMovingCount,
    DebrisMeanVelocity,
    DebrisMinSpeed,
    DebrisMaxSpeed,
    DebrisMeanSpin,
    DebrisTotalMass,
    DebrisDynamicsTime,
    SourceTime,
    EpochStart,
    EpochDuration,
    EpochIndex,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash)]
pub(crate) enum InstrumentGroup {
    Flight,
    Acceleration,
    Peaks,
    Actuation,
    Ship,
    Target,
    Stream,
    World,
    Mission,
}

impl InstrumentGroup {
    #[cfg(test)]
    pub(crate) const ALL: [Self; 9] = [
        Self::Flight,
        Self::Acceleration,
        Self::Peaks,
        Self::Actuation,
        Self::Ship,
        Self::Target,
        Self::Stream,
        Self::World,
        Self::Mission,
    ];

    pub(crate) fn title(self) -> &'static str {
        match self {
            Self::Flight => "Flight measurements",
            Self::Acceleration => "Observed acceleration",
            Self::Peaks => "Sampled peaks",
            Self::Actuation => "Applied hardware and forces",
            Self::Ship => "Lander state and cached wrench",
            Self::Target => "Target state",
            Self::Stream => "Measured asteroid dynamics",
            Self::World => "New world: replace this run",
            Self::Mission => "Mission totals and sample clock",
        }
    }

    pub(crate) fn instruments(self) -> &'static [Instrument] {
        use ActorField::*;
        use FlightMetric::*;
        use Instrument::*;
        match self {
            Self::Flight => &[
                Flight(Speed),
                Flight(TerrainClearance),
                Flight(Climb),
                Flight(TargetRange),
                Flight(Closing),
                Flight(ThrustAcceleration),
            ],
            Self::Acceleration => &[
                ObservedVector,
                ObservedMagnitude,
                IntervalStart,
                IntervalEnd,
                IntervalDuration,
            ],
            Self::Peaks => &[PeakSpeed, PeakClimb, PeakClearance],
            Self::Actuation => &[
                BodyMass,
                Ship(ActuatorGain),
                ActuationStatus,
                InertiaRow(Axis::X),
                InertiaRow(Axis::Y),
                InertiaRow(Axis::Z),
                Ship(RequestedAcceleration),
                Ship(RequestedAngularAcceleration),
                LastForce,
                Flight(Force),
                LastTorque,
                Flight(Torque),
                Flight(Spin),
                Ship(MaximumThrust),
                LastApplicationTime,
                LastApplicationRevision,
                TotalExternalForce,
                TotalExternalTorque,
            ],
            Self::Ship => &[
                Ship(Position),
                Ship(Velocity),
                Ship(Rotation),
                Ship(AngularVelocity),
                Ship(Radius),
                Ship(StoredFuel),
                Ship(Mass),
                Ship(AppliedAcceleration),
                Ship(AppliedForce),
                Ship(AppliedTorque),
            ],
            Self::Target => &[
                Target(Position),
                Target(Velocity),
                Target(Rotation),
                Target(AngularVelocity),
                Target(Mass),
                Target(Radius),
            ],
            Self::Stream => &[
                DebrisCount,
                DebrisMovingCount,
                DebrisMeanVelocity,
                DebrisMinSpeed,
                DebrisMaxSpeed,
                DebrisMeanSpin,
                DebrisTotalMass,
                DebrisDynamicsTime,
                StreamSpeed,
                BirthSpread,
                StreamDirection,
            ],
            Self::World => &[],
            Self::Mission => &[
                Flight(FuelUsed),
                Flight(Damage),
                Collisions,
                ActiveContacts,
                ManualInterventions,
                TuningRevision,
                SourceTime,
                EpochStart,
                EpochDuration,
                EpochIndex,
            ],
        }
    }
}

impl Instrument {
    pub(crate) fn label(self) -> &'static str {
        match self {
            Self::Flight(metric) => metric.strip_label(),
            Self::ObservedVector => "Mean frame acceleration · XYZ",
            Self::ObservedMagnitude => "Mean frame acceleration · magnitude",
            Self::IntervalStart => "Interval start",
            Self::IntervalEnd => "Interval end",
            Self::IntervalDuration => "Sample interval",
            Self::PeakSpeed => "Peak speed",
            Self::PeakClimb => "Peak climb · signed",
            Self::PeakClearance => "Peak terrain-center clearance",
            Self::Ship(field) | Self::Target(field) => match field {
                ActorField::Position => "Position · frame XYZ",
                ActorField::Velocity => "Velocity · frame XYZ",
                ActorField::Rotation => "Rotation · quaternion XYZW",
                ActorField::AngularVelocity => "Angular velocity · frame XYZ",
                ActorField::Mass => "Mass",
                ActorField::Radius => "Radius",
                ActorField::StoredFuel => "Stored fuel",
                ActorField::RequestedAcceleration => "Requested acceleration · body XYZ",
                ActorField::RequestedAngularAcceleration => "Requested angular accel · body XYZ",
                ActorField::AppliedAcceleration => "Snapshot wrench / mass · body XYZ",
                ActorField::AppliedForce => "Snapshot wrench force · body XYZ",
                ActorField::AppliedTorque => "Snapshot wrench torque · body XYZ",
                ActorField::MaximumThrust => "Stored hardware maximum thrust",
                ActorField::ActuatorGain => "Local thrust multiplier",
            },
            Self::InertiaRow(axis) => match axis {
                Axis::X => "Inertia · body X row",
                Axis::Y => "Inertia · body Y row",
                Axis::Z => "Inertia · body Z row",
            },
            Self::BodyMass => "Rapier body mass",
            Self::ActuationStatus => "Actuator application",
            Self::LastApplicationTime => "Last application · simulation time",
            Self::LastApplicationRevision => "Last application · tuning revision",
            Self::LastForce => "Last actuator force · body XYZ",
            Self::LastTorque => "Last actuator torque · body XYZ",
            Self::TotalExternalForce => "Rapier user force · frame XYZ",
            Self::TotalExternalTorque => "Rapier user torque · frame XYZ",
            Self::Collisions => "Collisions · accumulated",
            Self::ActiveContacts => "Active contacts",
            Self::ManualInterventions => "Recorded local interventions",
            Self::TuningRevision => "Applied tuning revision",
            Self::DebrisCount => "Physical asteroid count",
            Self::StreamSpeed => "Stream reference speed",
            Self::BirthSpread => "Birth speed spread",
            Self::StreamDirection => "Stream direction · frame axes",
            Self::DebrisMovingCount => "Moving asteroids · stored body velocity",
            Self::DebrisMeanVelocity => "Measured mean velocity · rotating frame XYZ",
            Self::DebrisMinSpeed => "Measured minimum speed",
            Self::DebrisMaxSpeed => "Measured maximum speed",
            Self::DebrisMeanSpin => "Measured mean angular speed",
            Self::DebrisTotalMass => "Measured total asteroid mass",
            Self::DebrisDynamicsTime => "Body motion sample · simulation time",
            Self::SourceTime => "Telemetry source time",
            Self::EpochStart => "Epoch start",
            Self::EpochDuration => "Observed epoch duration",
            Self::EpochIndex => "Epoch",
        }
    }

    pub(super) fn reading(self, report: &Report) -> Reading {
        use FlightMetric::*;
        match self {
            Self::Flight(metric) => {
                let value = report
                    .metrics
                    .inventory()
                    .into_iter()
                    .find_map(|(key, value)| (key == metric).then_some(value))
                    .flatten();
                let (unit, decimals) = match metric {
                    Speed | Climb | Closing => ("m/s", 2),
                    TerrainClearance | TargetRange => ("m", 1),
                    ThrustAcceleration => ("m/s²", 3),
                    Force => ("N", 1),
                    Torque => ("N m", 2),
                    Spin => ("rad/s", 4),
                    FuelUsed => ("kg", 3),
                    Damage => ("J", 1),
                };
                Reading::Scalar(value, unit, decimals)
            }
            Self::ObservedVector => {
                Reading::Vector(report.observed_frame_acceleration.vector_mps2, "m/s²", 4)
            }
            Self::ObservedMagnitude => {
                Reading::Scalar(report.observed_frame_acceleration.magnitude_mps2, "m/s²", 4)
            }
            Self::IntervalStart => {
                Reading::Scalar(report.observed_frame_acceleration.interval_start_s, "s", 3)
            }
            Self::IntervalEnd => {
                Reading::Scalar(report.observed_frame_acceleration.interval_end_s, "s", 3)
            }
            Self::IntervalDuration => {
                Reading::Scalar(report.observed_frame_acceleration.interval_s, "s", 3)
            }
            Self::PeakSpeed => Reading::Scalar(report.peaks.speed_mps, "m/s", 2),
            Self::PeakClimb => Reading::Scalar(report.peaks.radial_climb_mps, "m/s", 2),
            Self::PeakClearance => Reading::Scalar(report.peaks.terrain_center_clearance_m, "m", 1),
            Self::Ship(field) => field.reading(&report.ship),
            Self::Target(field) => field.reading(&report.target),
            Self::InertiaRow(axis) => Reading::Vector(
                report
                    .ship_inertia_body_kg_m2
                    .map(|matrix| matrix[axis.index()]),
                "kg m²",
                2,
            ),
            Self::BodyMass => Reading::Scalar(
                report
                    .applied_physics
                    .as_ref()
                    .map(|physics| physics.mass_kg),
                "kg",
                2,
            ),
            Self::ActuationStatus => {
                Reading::Text(report.applied_physics.as_ref().map(|physics| {
                    match physics.actuation_state {
                        crate::model::ActuationState::NotApplied => "not yet applied",
                        crate::model::ActuationState::AwaitingApplication => {
                            "request awaiting application"
                        }
                        crate::model::ActuationState::Coasting => "coasting · zero actuator force",
                        crate::model::ActuationState::Actuating => "nonzero force applied",
                    }
                }))
            }
            Self::LastApplicationTime => Reading::Scalar(
                report
                    .applied_physics
                    .as_ref()
                    .and_then(|physics| physics.last_actuator_application)
                    .map(|last| last.application_time_s),
                "s",
                3,
            ),
            Self::LastApplicationRevision => Reading::Count(
                report
                    .applied_physics
                    .as_ref()
                    .and_then(|physics| physics.last_actuator_application)
                    .map(|last| u64::from(last.tuning_revision)),
            ),
            Self::LastForce => Reading::Vector(
                report
                    .applied_physics
                    .as_ref()
                    .and_then(|physics| physics.last_actuator_application)
                    .map(|last| last.force_body_n),
                "N",
                2,
            ),
            Self::LastTorque => Reading::Vector(
                report
                    .applied_physics
                    .as_ref()
                    .and_then(|physics| physics.last_actuator_application)
                    .map(|last| last.torque_body_nm),
                "N m",
                2,
            ),
            Self::TotalExternalForce => Reading::Vector(
                report
                    .applied_physics
                    .as_ref()
                    .map(|physics| physics.total_external_force_world_n),
                "N",
                2,
            ),
            Self::TotalExternalTorque => Reading::Vector(
                report
                    .applied_physics
                    .as_ref()
                    .map(|physics| physics.total_external_torque_world_nm),
                "N m",
                2,
            ),
            Self::Collisions => Reading::Count(report.mission.collisions.map(u64::from)),
            Self::ActiveContacts => {
                Reading::Count(report.mission.active_contacts.map(|value| value as u64))
            }
            Self::ManualInterventions => {
                Reading::Count(report.mission.manual_interventions.map(u64::from))
            }
            Self::TuningRevision => Reading::Count(report.mission.tuning_revision.map(u64::from)),
            Self::DebrisCount => Reading::Count(
                report
                    .debris_dynamics
                    .as_ref()
                    .map(|sample| sample.physical_count)
                    .or(report.physical_debris_count)
                    .map(|value| value as u64),
            ),
            Self::DebrisMovingCount => Reading::Count(
                report
                    .debris_dynamics
                    .as_ref()
                    .map(|sample| sample.moving_count as u64),
            ),
            Self::DebrisMeanVelocity => Reading::Vector(
                report
                    .debris_dynamics
                    .as_ref()
                    .map(|sample| sample.mean_velocity_world_mps),
                "m/s",
                3,
            ),
            Self::DebrisMinSpeed => Reading::Scalar(
                report
                    .debris_dynamics
                    .as_ref()
                    .and_then(|sample| sample.min_speed_mps),
                "m/s",
                3,
            ),
            Self::DebrisMaxSpeed => Reading::Scalar(
                report
                    .debris_dynamics
                    .as_ref()
                    .and_then(|sample| sample.max_speed_mps),
                "m/s",
                3,
            ),
            Self::DebrisMeanSpin => Reading::Scalar(
                report
                    .debris_dynamics
                    .as_ref()
                    .and_then(|sample| sample.mean_spin_rad_s),
                "rad/s",
                4,
            ),
            Self::DebrisTotalMass => Reading::Scalar(
                report
                    .debris_dynamics
                    .as_ref()
                    .map(|sample| sample.total_mass_kg),
                "kg",
                2,
            ),
            Self::DebrisDynamicsTime => Reading::Scalar(
                report
                    .debris_dynamics
                    .as_ref()
                    .map(|sample| sample.source_time_s),
                "s",
                3,
            ),
            Self::StreamSpeed => Reading::Scalar(
                report.debris_flow_reference.map(|flow| flow.speed_mps),
                "m/s",
                2,
            ),
            Self::BirthSpread => Reading::Scalar(
                report
                    .debris_flow_reference
                    .map(|flow| flow.speed_spread_mps),
                "m/s",
                2,
            ),
            Self::StreamDirection => Reading::Direction(
                report
                    .debris_flow_reference
                    .map(|flow| (flow.stream_axis, flow.stream_sign)),
            ),
            Self::SourceTime => Reading::Scalar(report.source_time_s, "s", 3),
            Self::EpochStart => Reading::Scalar(report.epoch.started_at_time_s, "s", 3),
            Self::EpochDuration => Reading::Scalar(report.epoch.observed_duration_s, "s", 3),
            Self::EpochIndex => {
                Reading::Count(report.epoch.started_at_time_s.map(|_| report.epoch.index))
            }
        }
    }
}

impl ActorField {
    fn reading(self, actor: &ActorMeasurements) -> Reading {
        match self {
            Self::Position => Reading::Vector(actor.position_m, "m", 2),
            Self::Velocity => Reading::Vector(actor.velocity_mps, "m/s", 3),
            Self::Rotation => Reading::Quaternion(actor.rotation_xyzw),
            Self::AngularVelocity => Reading::Vector(actor.angular_velocity_rad_s, "rad/s", 4),
            Self::Mass => Reading::Scalar(actor.mass_kg, "kg", 2),
            Self::Radius => Reading::Scalar(actor.radius_m, "m", 2),
            Self::StoredFuel => Reading::Scalar(actor.fuel_kg, "kg", 3),
            Self::RequestedAcceleration => {
                Reading::Vector(actor.requested_accel_body_mps2, "m/s²", 4)
            }
            Self::RequestedAngularAcceleration => {
                Reading::Vector(actor.requested_angular_accel_body_rad_s2, "rad/s²", 4)
            }
            Self::AppliedAcceleration => Reading::Vector(actor.applied_accel_body_mps2, "m/s²", 4),
            Self::AppliedForce => Reading::Vector(actor.applied_force_body_n, "N", 2),
            Self::AppliedTorque => Reading::Vector(actor.applied_torque_body_nm, "N m", 2),
            Self::MaximumThrust => Reading::Scalar(actor.max_thrust_n, "N", 1),
            Self::ActuatorGain => Reading::Scalar(actor.actuator_gain, "×", 2),
        }
    }
}

pub(super) enum Reading {
    Text(Option<&'static str>),
    Scalar(Option<f64>, &'static str, usize),
    Vector(Option<[f64; 3]>, &'static str, usize),
    Quaternion(Option<[f64; 4]>),
    Count(Option<u64>),
    Direction(Option<(crate::model::StreamAxis, i8)>),
}
