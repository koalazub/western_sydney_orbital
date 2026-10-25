//! Placement state and transitions. No session dispatch or rendering effects.
use super::SurfaceStep;
use crate::{api::CommandContext, domain::EntityId, model::Snapshot, terrain::LandingSite};
use bevy::prelude::Resource;

pub(super) const NUDGE_METRES: f64 = 100.0;
pub(super) const DRAG_THRESHOLD_PX: f32 = 4.0;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub(super) enum Pointer {
    Mouse,
    Touch(u64),
}

#[derive(Clone, Copy, Debug, Default)]
pub(super) struct ActorIndices {
    pub pad: Option<usize>,
    pub lander: Option<usize>,
    checked: Option<(u32, usize)>,
}

impl ActorIndices {
    pub fn refresh(&mut self, snapshot: &Snapshot, revision: u32) {
        // Stable snapshot indices avoid a debris-sized scan on pointer movement.
        // Cache missing actors too, until the trial or asset layout changes.
        let key = (revision, snapshot.assets.len());
        for (index, id) in [
            (&mut self.pad, EntityId::LandingPad),
            (&mut self.lander, EntityId::Lander),
        ] {
            if index.is_some_and(|index| snapshot.assets.get(index).is_none_or(|a| a.id != id))
                || (index.is_none() && self.checked != Some(key))
            {
                *index = snapshot.assets.iter().position(|asset| asset.id == id);
            }
        }
        self.checked = Some(key);
    }
}

#[derive(Clone, Copy, Debug, PartialEq)]
struct ActorContext {
    position: [f64; 3],
    rotation: [f64; 4],
    velocity: [f64; 3],
    angular_velocity: [f64; 3],
    command: [f64; 3],
    angular_command: [f64; 3],
}

impl From<&crate::model::EntityState> for ActorContext {
    fn from(actor: &crate::model::EntityState) -> Self {
        Self {
            position: actor.position,
            rotation: actor.rotation,
            velocity: actor.velocity,
            angular_velocity: actor.angular_velocity_rad_s,
            command: actor.commanded_accel_mps2,
            angular_command: actor.commanded_angular_accel_rad_s2,
        }
    }
}

#[derive(Clone, Copy, Debug)]
pub(super) struct Source {
    pub expected: CommandContext,
    time_s: f64,
    pad: ActorContext,
    lander: ActorContext,
    indices: ActorIndices,
}

impl Source {
    pub fn capture(
        snapshot: &Snapshot,
        indices: ActorIndices,
        trial_revision: u32,
        running: bool,
    ) -> Option<Self> {
        let pad = snapshot.assets.get(indices.pad?)?;
        let lander = snapshot.assets.get(indices.lander?)?;
        if pad.id != EntityId::LandingPad || lander.id != EntityId::Lander {
            return None;
        }
        Some(Self {
            expected: CommandContext {
                trial_revision,
                tuning_revision: snapshot.tuning_revision,
                manual_interventions: snapshot.manual_interventions,
                seed: snapshot.seed,
                running,
            },
            time_s: snapshot.time_s,
            pad: pad.into(),
            lander: lander.into(),
            indices,
        })
    }

    pub fn matches(&self, snapshot: &Snapshot, trial_revision: u32, running: bool) -> bool {
        let Some(current) = Self::capture(snapshot, self.indices, trial_revision, running) else {
            return false;
        };
        self.expected.trial_revision == current.expected.trial_revision
            && self.expected.tuning_revision == current.expected.tuning_revision
            && self.expected.manual_interventions == current.expected.manual_interventions
            && self.expected.seed == current.expected.seed
            && self.expected.running == current.expected.running
            && self.time_s == current.time_s
            && self.pad == current.pad
            && self.lander == current.lander
    }

    fn after_pause(
        self,
        snapshot: &Snapshot,
        trial_revision: u32,
        running: bool,
        release_command: bool,
    ) -> Option<Self> {
        let mut released = self;
        released.lander.command = [0.0; 3];
        released.lander.angular_command = [0.0; 3];
        // The real runtime pause can release a command owned by the keyboard.
        // Accept only that release, never a changed pose, time, trial or tuning.
        if self.matches(snapshot, trial_revision, running)
            || (release_command && released.matches(snapshot, trial_revision, running))
        {
            Self::capture(snapshot, self.indices, trial_revision, running)
        } else {
            None
        }
    }
}

#[derive(Clone, Copy, Debug)]
pub(super) struct Preview {
    pub direction: [f64; 3],
    pub site: LandingSite,
}

#[derive(Clone, Copy, Debug)]
pub(super) struct Edit {
    pub source: Source,
    pub preview: Option<Preview>,
}

#[derive(Clone, Copy, Debug, Default)]
pub(super) enum Phase {
    #[default]
    Idle,
    Preparing {
        edit: Edit,
        release_command: bool,
    },
    Armed(Edit),
    Dragging {
        edit: Edit,
        pointer: Pointer,
        start: [f32; 2],
        grab_offset: [f32; 2],
        moved: bool,
    },
}

#[derive(Clone, Copy, Debug)]
pub(super) struct Commit {
    pub direction: [f64; 3],
    pub expected: CommandContext,
}

#[derive(Clone, Copy, Default)]
enum ReleaseGuard {
    #[default]
    Clear,
    Waiting,
    LastFrame,
}

#[derive(Resource, Default)]
pub(super) struct Placement {
    pub phase: Phase,
    pub indices: ActorIndices,
    pub notice: Option<String>,
    releasing: ReleaseGuard,
}

impl Placement {
    pub fn active(&self) -> bool {
        !matches!(self.phase, Phase::Idle)
    }

    pub fn preparing(&self) -> bool {
        matches!(self.phase, Phase::Preparing { .. })
    }

    pub fn blocked(&self) -> bool {
        self.active() || !matches!(self.releasing, ReleaseGuard::Clear)
    }

    /// Call once at the beginning of an input frame. A transition cannot clear
    /// its own guard; neither can the final physical key/button-up frame.
    pub fn begin_frame(&mut self, held: bool, released: bool) {
        self.releasing = match self.releasing {
            ReleaseGuard::Clear | ReleaseGuard::LastFrame => ReleaseGuard::Clear,
            ReleaseGuard::Waiting if held => ReleaseGuard::Waiting,
            ReleaseGuard::Waiting if released => ReleaseGuard::LastFrame,
            ReleaseGuard::Waiting => ReleaseGuard::Clear,
        };
    }

    pub fn end_frame(&mut self, held: bool) {
        if matches!(self.releasing, ReleaseGuard::Waiting) && !held {
            // The terminal event itself may already be the last release. Keep
            // this frame blocked, but permit a fresh key press next frame.
            self.releasing = ReleaseGuard::LastFrame;
        }
    }

    pub fn edit(&self) -> Option<&Edit> {
        match &self.phase {
            Phase::Idle => None,
            Phase::Preparing { edit, .. } | Phase::Armed(edit) | Phase::Dragging { edit, .. } => {
                Some(edit)
            }
        }
    }

    pub fn edit_mut(&mut self) -> Option<&mut Edit> {
        match &mut self.phase {
            Phase::Idle => None,
            Phase::Preparing { edit, .. } | Phase::Armed(edit) | Phase::Dragging { edit, .. } => {
                Some(edit)
            }
        }
    }

    pub fn prepare(&mut self, source: Source, preview: Preview, release_command: bool) {
        self.phase = Phase::Preparing {
            edit: Edit {
                source,
                preview: Some(preview),
            },
            release_command,
        };
        self.notice = None;
    }

    pub fn rebase_preparation(
        &mut self,
        snapshot: &Snapshot,
        trial_revision: u32,
        running: bool,
    ) -> bool {
        let Phase::Preparing {
            edit,
            release_command,
        } = &mut self.phase
        else {
            return false;
        };
        let Some(source) =
            edit.source
                .after_pause(snapshot, trial_revision, running, *release_command)
        else {
            return false;
        };
        edit.source = source;
        *release_command = false;
        true
    }

    pub fn finish_preparation(&mut self) {
        if let Phase::Preparing { edit, .. } = self.phase {
            self.phase = Phase::Armed(edit);
            self.notice = None;
        }
    }

    pub fn arm(&mut self, source: Source) {
        self.phase = Phase::Armed(Edit {
            source,
            preview: None,
        });
        self.notice = None;
    }

    pub fn start_drag(
        &mut self,
        source: Source,
        preview: Preview,
        pointer: Pointer,
        at: [f32; 2],
        surface_anchor: [f32; 2],
    ) {
        self.phase = Phase::Dragging {
            edit: Edit {
                source,
                preview: Some(preview),
            },
            pointer,
            start: at,
            grab_offset: [at[0] - surface_anchor[0], at[1] - surface_anchor[1]],
            moved: false,
        };
        self.notice = None;
    }

    pub fn drag_pointer(&self) -> Option<Pointer> {
        match self.phase {
            Phase::Dragging { pointer, .. } => Some(pointer),
            Phase::Idle | Phase::Preparing { .. } | Phase::Armed(_) => None,
        }
    }

    pub fn surface_cursor(&self, at: [f32; 2]) -> [f32; 2] {
        match self.phase {
            Phase::Dragging { grab_offset, .. } => [at[0] - grab_offset[0], at[1] - grab_offset[1]],
            Phase::Idle | Phase::Preparing { .. } | Phase::Armed(_) => at,
        }
    }

    pub fn move_drag(&mut self, pointer: Pointer, at: [f32; 2], preview: Option<Preview>) {
        if let Phase::Dragging {
            edit,
            pointer: owner,
            start,
            moved,
            ..
        } = &mut self.phase
            && *owner == pointer
        {
            let distance_squared = (at[0] - start[0]).powi(2) + (at[1] - start[1]).powi(2);
            *moved |= distance_squared > DRAG_THRESHOLD_PX.powi(2);
            if *moved {
                edit.preview = preview;
            }
        }
    }

    pub fn cancel(&mut self, notice: impl Into<String>) {
        self.phase = Phase::Idle;
        self.releasing = ReleaseGuard::Waiting;
        self.notice = Some(notice.into());
    }

    pub fn finish_drag(&mut self, pointer: Pointer, valid_release: bool) -> Option<Commit> {
        let Phase::Dragging {
            edit,
            pointer: owner,
            moved,
            ..
        } = self.phase
        else {
            return None;
        };
        if owner != pointer {
            return None;
        }
        if !moved {
            self.cancel("Pad unchanged. Drag its lift grip or choose Place lander.");
            return None;
        }
        if !valid_release || edit.preview.is_none() {
            self.cancel("Placement cancelled. Release on visible Phobos terrain to move the pad.");
            return None;
        }
        self.take_commit()
    }

    pub fn take_commit(&mut self) -> Option<Commit> {
        if self.preparing() {
            return None;
        }
        let edit = *self.edit()?;
        let Some(preview) = edit.preview else {
            self.notice =
                Some("Choose a point on Phobos or nudge the preview before Apply.".into());
            return None;
        };
        // Consume the edit before dispatch, including on rejection. Another
        // release/Enter/Confirm in this frame cannot send the operation twice.
        self.phase = Phase::Idle;
        self.releasing = ReleaseGuard::Waiting;
        self.notice = None;
        Some(Commit {
            direction: preview.direction,
            expected: edit.source.expected,
        })
    }

    pub fn nudge(&mut self, step: SurfaceStep, initial: Preview) -> Result<(), String> {
        let current = self.edit().and_then(|edit| edit.preview).unwrap_or(initial);
        let preview = super::geometry::nudge(current, step)?;
        if let Some(edit) = self.edit_mut() {
            edit.preview = Some(preview);
            self.notice = None;
        }
        Ok(())
    }
}
