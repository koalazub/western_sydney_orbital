use crate::model::{
    DebrisDistribution, DebrisFlowTuning, ExperimentSettings, FlightPreset, ShipTuning, Snapshot,
    StreamAxis,
};
use bevy::prelude::*;
use std::collections::VecDeque;

/// Focus contract for the session and camera. Includes editor action buttons so
/// Space on a world/placement control cannot also launch the lander.
#[derive(Component)]
pub(crate) struct InstrumentEditor;

/// Only physical inputs belong here. Measured speed, force, torque, fuel,
/// damage and contact counters deliberately have no editable variant.
#[derive(Component, Clone, Copy, Debug, PartialEq, Eq)]
pub(crate) enum EditorField {
    Mass,
    InertiaFactor,
    ThrustGain,
    StreamSpeed,
    StreamSpread,
    WorldCount,
    RadiusMin,
    RadiusMax,
    ShellInner,
    ShellOuter,
    Placement(StreamAxis),
    ImpactSource(StreamAxis),
    ImpactKick(StreamAxis),
    ImpactTangentialSpeed,
    ImpactPatchAngle,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub(super) enum EditorScope {
    Ship,
    Flow,
    World,
    Placement,
}

impl EditorField {
    pub const SHIP: [Self; 3] = [Self::Mass, Self::InertiaFactor, Self::ThrustGain];
    pub const FLOW: [Self; 2] = [Self::StreamSpeed, Self::StreamSpread];
    pub const WORLD: [Self; 5] = [
        Self::WorldCount,
        Self::RadiusMin,
        Self::RadiusMax,
        Self::ShellInner,
        Self::ShellOuter,
    ];
    pub const PLACEMENT: [Self; 3] = [
        Self::Placement(StreamAxis::X),
        Self::Placement(StreamAxis::Y),
        Self::Placement(StreamAxis::Z),
    ];
    pub const IMPACT: [Self; 8] = [
        Self::ImpactSource(StreamAxis::X),
        Self::ImpactSource(StreamAxis::Y),
        Self::ImpactSource(StreamAxis::Z),
        Self::ImpactKick(StreamAxis::X),
        Self::ImpactKick(StreamAxis::Y),
        Self::ImpactKick(StreamAxis::Z),
        Self::ImpactTangentialSpeed,
        Self::ImpactPatchAngle,
    ];

    pub fn label(self) -> &'static str {
        match self {
            Self::Mass => "Mass input [kg]",
            Self::InertiaFactor => "Inertia factor [x]",
            Self::ThrustGain => "Local thrust gain [x]",
            Self::StreamSpeed => "Stream reference speed input [m/s]",
            Self::StreamSpread => "Birth speed spread input [m/s]",
            Self::WorldCount => "New world asteroid count",
            Self::RadiusMin => "Minimum asteroid radius [m]",
            Self::RadiusMax => "Maximum asteroid radius [m]",
            Self::ShellInner => "Inner shell terrain clearance [m]",
            Self::ShellOuter => "Outer shell terrain clearance [m]",
            Self::Placement(StreamAxis::X) => "Lander-only X [m]",
            Self::Placement(StreamAxis::Y) => "Lander-only Y [m]",
            Self::Placement(StreamAxis::Z) => "Lander-only Z [m]",
            Self::ImpactSource(StreamAxis::X) => "Ejection source direction X",
            Self::ImpactSource(StreamAxis::Y) => "Ejection source direction Y",
            Self::ImpactSource(StreamAxis::Z) => "Ejection source direction Z",
            Self::ImpactKick(StreamAxis::X) => "Initial world kick X [m/s]",
            Self::ImpactKick(StreamAxis::Y) => "Initial world kick Y [m/s]",
            Self::ImpactKick(StreamAxis::Z) => "Initial world kick Z [m/s]",
            Self::ImpactTangentialSpeed => "Initial tangential speed [m/s]",
            Self::ImpactPatchAngle => "Source patch half-angle [deg]",
        }
    }

    pub fn hint(self) -> &'static str {
        match self {
            Self::Mass => "100 to 100000 kg. Applies to the actual lander body.",
            Self::InertiaFactor => "0.1 to 10. Scales inertia after mass scaling, not speed.",
            Self::ThrustGain => {
                "0.1 to 10. Local force and torque gain, not the reference command cap."
            }
            Self::StreamSpeed => {
                "0.1 to 200 m/s. Changes the bulk stream by a live velocity delta."
            }
            Self::StreamSpread => "0 to half the reference speed. New births only.",
            Self::WorldCount => {
                "Whole number, 0 to 100000. 1e5 is accepted. Large worlds can be slow."
            }
            Self::RadiusMin | Self::RadiusMax => "0.05 to 100 m. Minimum must not exceed maximum.",
            Self::ShellInner | Self::ShellOuter => {
                "0 to 50000 m above terrain, plus asteroid radius. At least 10 m thick."
            }
            Self::Placement(_) => {
                "Phobos-centered rotating world axes, in meters. Debug-only draft until Apply XYZ position."
            }
            Self::ImpactSource(_) => {
                "Source direction from Phobos center. XYZ must form a nonzero finite vector."
            }
            Self::ImpactKick(_) => {
                "Initial rotating-frame velocity. Combined XYZ speed at most 200 m/s."
            }
            Self::ImpactTangentialSpeed => {
                "0 to 200 m/s. Initial ejection input, not an orbital-speed measurement."
            }
            Self::ImpactPatchAngle => "Greater than 0, at most 180 degrees. New world only.",
        }
    }

    pub(super) fn commit_label(self) -> &'static str {
        match self.scope() {
            EditorScope::Ship | EditorScope::Flow => "Apply",
            EditorScope::World | EditorScope::Placement => "Stage",
        }
    }

    pub(super) fn scope(self) -> EditorScope {
        match self {
            Self::Mass | Self::InertiaFactor | Self::ThrustGain => EditorScope::Ship,
            Self::StreamSpeed | Self::StreamSpread => EditorScope::Flow,
            Self::WorldCount
            | Self::RadiusMin
            | Self::RadiusMax
            | Self::ShellInner
            | Self::ShellOuter
            | Self::ImpactSource(_)
            | Self::ImpactKick(_)
            | Self::ImpactTangentialSpeed
            | Self::ImpactPatchAngle => EditorScope::World,
            Self::Placement(_) => EditorScope::Placement,
        }
    }

    pub fn step(self) -> f64 {
        match self {
            Self::Mass => 100.0,
            Self::InertiaFactor
            | Self::ThrustGain
            | Self::StreamSpread
            | Self::RadiusMin
            | Self::RadiusMax
            | Self::ImpactSource(_)
            | Self::ImpactKick(_)
            | Self::ImpactTangentialSpeed => 0.1,
            Self::StreamSpeed | Self::ImpactPatchAngle => 1.0,
            Self::WorldCount => 100.0,
            Self::ShellInner | Self::ShellOuter | Self::Placement(_) => 10.0,
        }
    }

    pub fn parse(self, text: &str) -> Result<f64, String> {
        let value = text
            .trim()
            .parse::<f64>()
            .map_err(|_| "Enter a finite number, such as 1000 or 1e3.".to_owned())?;
        if !value.is_finite() {
            return Err("Enter a finite number, not NaN or infinity.".into());
        }
        if self == Self::WorldCount
            && (value.fract() != 0.0
                || !(0.0..=crate::model::MAX_DEBRIS_COUNT as f64).contains(&value))
        {
            return Err("Enter a whole asteroid count from 0 to 100000.".into());
        }
        Ok(value)
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub(super) enum StepDirection {
    Decrease,
    Increase,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub(super) enum DistributionChoice {
    Stream,
    Impact,
}

#[derive(Component, Clone, Copy, Debug, PartialEq, Eq)]
pub(super) enum EditorAction {
    Commit(EditorField),
    Nudge(EditorField, StepDirection),
    FlowAxis(StreamAxis),
    ReverseFlow,
    Preset(FlightPreset),
    Distribution(DistributionChoice),
    ToggleAdvanced,
    ResetImpact,
    AskCreateWorld,
    CancelCreateWorld,
    CreateWorld,
    PlaceLander,
    ReloadPlacement,
}

#[derive(Clone, Copy)]
pub(super) enum EditorIntent {
    Commit(Entity, EditorField),
    Cancel(Entity, EditorField),
    Action(EditorAction),
}

#[derive(Clone, Copy, Default)]
pub(super) enum EditSession {
    #[default]
    Idle,
    Editing {
        entity: Entity,
        field: EditorField,
    },
}

#[derive(Clone, Copy, Default, PartialEq, Eq)]
pub(super) enum Disclosure {
    #[default]
    Collapsed,
    Expanded,
}

#[derive(Clone, Copy, Default, PartialEq, Eq)]
pub(super) enum Reload {
    #[default]
    None,
    Live,
    All,
}

#[derive(Clone, Debug)]
pub(super) enum InputNotice {
    Staged,
    Applied,
    Rejected(String),
    Diverged(String),
}

impl InputNotice {
    pub fn text(&self) -> String {
        match self {
            Self::Staged => "Draft only. Apply with the explicit button below.".into(),
            Self::Applied => "Applied. Values reloaded from the current world.".into(),
            Self::Rejected(error) => format!(
                "Not applied. {} Confirmed values reloaded.",
                plain_error(error)
            ),
            Self::Diverged(error) => format!(
                "Current world changed. {} Values reloaded.",
                plain_error(error)
            ),
        }
    }
}

pub(super) fn plain_error(error: &str) -> String {
    let mut message = error.to_owned();
    for (field, label) in [
        ("ship.mass_kg", "Mass"),
        ("ship.inertia_scale", "Inertia factor"),
        ("ship.thrust_scale", "Local thrust gain"),
        ("debris.count", "Asteroid count"),
        ("debris.radius_min_m", "Minimum radius"),
        ("debris.radius_max_m", "Maximum radius"),
        ("debris.speed_spread_mps", "Birth speed spread"),
        ("debris.speed_mps", "Stream reference speed"),
        ("debris.stream_sign", "Stream sign"),
        (
            "debris impact source_direction",
            "Ejection source direction",
        ),
        ("debris impact kick_velocity_mps", "Initial kick velocity"),
        (
            "debris impact tangential_speed_mps",
            "Initial tangential speed",
        ),
        (
            "debris impact patch_half_angle_deg",
            "Source patch half-angle",
        ),
        ("shell_inner_m", "inner shell clearance"),
        ("shell_outer_m", "outer shell clearance"),
    ] {
        message = message.replace(field, label);
    }
    if !message.ends_with('.') {
        message.push('.');
    }
    message
}

#[derive(Clone, PartialEq)]
pub(super) struct ConfirmedInputs {
    pub seed: u64,
    pub preset: FlightPreset,
    pub experiment: Option<ExperimentSettings>,
    pub ship: ShipTuning,
    pub flow: DebrisFlowTuning,
    pub revision: u32,
    pub interventions: u32,
    pub time_s: f64,
}

impl From<&Snapshot> for ConfirmedInputs {
    fn from(snapshot: &Snapshot) -> Self {
        Self {
            seed: snapshot.seed,
            preset: snapshot.flight_preset,
            experiment: snapshot.experiment_settings,
            ship: snapshot.ship_tuning(),
            flow: snapshot.debris_flow(),
            revision: snapshot.tuning_revision,
            interventions: snapshot.manual_interventions,
            time_s: snapshot.time_s,
        }
    }
}

impl ConfirmedInputs {
    pub fn stream_active(&self) -> bool {
        self.experiment
            .is_none_or(|settings| settings.debris.distribution.is_directed_stream())
    }

    pub fn world_draft(&self) -> WorldDraft {
        let mut settings = self.experiment.unwrap_or_default();
        settings.ship.mass_kg = self.ship.mass_kg;
        settings.ship.inertia_scale = self.ship.inertia_scale;
        settings.debris = self.flow.apply_to(settings.debris);
        WorldDraft {
            settings,
            preset: self.preset,
        }
    }
}

#[derive(Clone, Copy)]
pub(super) struct WorldDraft {
    pub settings: ExperimentSettings,
    pub preset: FlightPreset,
}

#[derive(Default, Resource)]
pub(super) struct InstrumentEditors {
    pub confirmed: Option<ConfirmedInputs>,
    pub world_revision: Option<u32>,
    pub world: Option<WorldDraft>,
    pub placement: Option<[f64; 3]>,
    pub session: EditSession,
    pub intents: VecDeque<EditorIntent>,
    pub field_notices: Vec<(EditorField, InputNotice)>,
    pub scope_notices: Vec<(EditorScope, InputNotice)>,
    pub advanced: Disclosure,
    pub create_confirmation: Disclosure,
    pub reload: Reload,
}

impl InstrumentEditors {
    pub fn notice_field(&mut self, field: EditorField, notice: InputNotice) {
        self.field_notices.retain(|(key, _)| *key != field);
        self.field_notices.push((field, notice));
    }

    pub fn notice_scope(&mut self, scope: EditorScope, notice: InputNotice) {
        self.scope_notices.retain(|(key, _)| *key != scope);
        self.scope_notices.push((scope, notice));
    }

    pub fn value(&self, field: EditorField) -> Option<f64> {
        match field {
            EditorField::Mass => self.confirmed.as_ref().map(|value| value.ship.mass_kg),
            EditorField::InertiaFactor => self
                .confirmed
                .as_ref()
                .map(|value| value.ship.inertia_scale),
            EditorField::ThrustGain => self.confirmed.as_ref().map(|value| value.ship.thrust_scale),
            EditorField::StreamSpeed => self.confirmed.as_ref().map(|value| value.flow.speed_mps),
            EditorField::StreamSpread => self
                .confirmed
                .as_ref()
                .map(|value| value.flow.speed_spread_mps),
            EditorField::WorldCount => self.world.map(|draft| draft.settings.debris.count as f64),
            EditorField::RadiusMin => self.world.map(|draft| draft.settings.debris.radius_min_m),
            EditorField::RadiusMax => self.world.map(|draft| draft.settings.debris.radius_max_m),
            EditorField::ShellInner => self.world.map(|draft| draft.settings.debris.shell_inner_m),
            EditorField::ShellOuter => self.world.map(|draft| draft.settings.debris.shell_outer_m),
            EditorField::Placement(axis) => self.placement.map(|position| position[axis.index()]),
            EditorField::ImpactSource(axis) => self
                .impact()
                .map(|settings| settings.source_direction[axis.index()]),
            EditorField::ImpactKick(axis) => self
                .impact()
                .map(|settings| settings.kick_velocity_mps[axis.index()]),
            EditorField::ImpactTangentialSpeed => {
                self.impact().map(|settings| settings.tangential_speed_mps)
            }
            EditorField::ImpactPatchAngle => {
                self.impact().map(|settings| settings.patch_half_angle_deg)
            }
        }
    }

    pub fn impact(&self) -> Option<crate::model::ImpactEjectaSettings> {
        match self.world?.settings.debris.distribution {
            DebrisDistribution::DirectedStream => None,
            DebrisDistribution::ImpactEjecta { settings } => Some(settings),
        }
    }
}

#[derive(Component, Default)]
pub(super) struct EditorBuffer(pub String);

#[derive(Component)]
pub(super) enum EditorReadout {
    Field(EditorField),
    Scope(EditorScope),
    FlowSummary,
    WorldSummary,
    ImpactSummary,
    PlacementSummary,
}

#[derive(Component)]
pub(super) enum EditorDisclosure {
    Advanced,
    Impact,
    CreateConfirmation,
}
