use super::{Movement, MovementTarget, PilotContext, PilotEffect};

#[derive(Default)]
pub(crate) struct KeyboardPilot {
    state: PilotState,
}

#[derive(Default)]
enum PilotState {
    #[default]
    Ready,
    CommandOwned,
    AwaitingKeyRelease,
    PendingCommandRelease,
}

impl KeyboardPilot {
    /// A fresh, authorized Space press explicitly starts a new manual launch.
    /// This is not called for held keys, focus recovery or modifier handoffs.
    pub(crate) fn rearm_for_launch(&mut self) {
        self.state = PilotState::Ready;
    }

    pub(crate) fn update(
        &mut self,
        movement: Movement,
        context: PilotContext,
        max_accel_mps2: f64,
    ) -> PilotEffect {
        if !context.can_control {
            // We cannot write into a view-only world, but the last force is
            // still installed. Clear it before physics resumes on regaining
            // authority, unless an external command/reset supersedes it first.
            self.state = if self.has_installed_command() {
                PilotState::PendingCommandRelease
            } else {
                PilotState::AwaitingKeyRelease
            };
            return PilotEffect::None;
        }

        let ready = context.active && context.running && context.mission_running && !context.modal;
        if self.requires_release() || (!ready && (movement.held || self.owns_command())) {
            let effect = self.release_command();
            // A paused held key also needs a release. Zero net axes are not a release.
            self.state = if !movement.held && context.active && !context.modal {
                PilotState::Ready
            } else {
                PilotState::AwaitingKeyRelease
            };
            return effect;
        }
        if !ready || !movement.held {
            return self.release_command();
        }

        match movement.target {
            MovementTarget::Ship => match bounded_command(movement.axes, max_accel_mps2) {
                Some(command) => {
                    self.state = PilotState::CommandOwned;
                    PilotEffect::SetCommand(command)
                }
                None => self.release_command(),
            },
            // Alt handoff does not inhibit a deliberate return with held keys.
            MovementTarget::DebugCamera | MovementTarget::Suppressed => self.release_command(),
        }
    }

    /// Forget ownership without releasing a command installed by an external source.
    pub(crate) fn cancel_until_release(&mut self) {
        self.state = PilotState::AwaitingKeyRelease;
    }

    /// Rebinding suspends manual input but does not forget an installed force.
    /// The next authorized runtime update releases only our own command once.
    pub(crate) fn block_until_release(&mut self) {
        self.state = if self.has_installed_command() {
            PilotState::PendingCommandRelease
        } else {
            PilotState::AwaitingKeyRelease
        };
    }

    pub(crate) fn owns_command(&self) -> bool {
        matches!(self.state, PilotState::CommandOwned)
    }

    pub(crate) fn has_installed_command(&self) -> bool {
        matches!(
            self.state,
            PilotState::CommandOwned | PilotState::PendingCommandRelease
        )
    }

    pub(crate) fn requires_release(&self) -> bool {
        matches!(
            self.state,
            PilotState::AwaitingKeyRelease | PilotState::PendingCommandRelease
        )
    }

    fn release_command(&mut self) -> PilotEffect {
        if self.has_installed_command() {
            self.state = PilotState::Ready;
            PilotEffect::ReleaseCommand
        } else {
            PilotEffect::None
        }
    }
}

fn bounded_command(axes: [f64; 3], max_accel_mps2: f64) -> Option<[f64; 3]> {
    if !max_accel_mps2.is_finite()
        || max_accel_mps2 <= 0.0
        || axes.iter().any(|axis| !axis.is_finite())
    {
        return None;
    }
    let length = magnitude(axes);
    if !length.is_finite() || length == 0.0 {
        return None;
    }
    let mut command = axes.map(|axis| (axis / length) * max_accel_mps2);
    // Floating-point normalization must not round a diagonal over the limit.
    while magnitude(command) > max_accel_mps2 {
        command = command.map(|axis| axis.signum() * axis.abs().next_down().max(0.0));
    }
    (command != [0.0; 3]).then_some(command)
}

fn magnitude([forward, left, up]: [f64; 3]) -> f64 {
    forward.hypot(left).hypot(up)
}

#[cfg(test)]
mod tests {
    use super::magnitude;
    use crate::controls::{
        KeyboardPilot, Movement, MovementTarget, PilotContext, PilotEffect, decode,
    };

    const W: u16 = 1 << 0;
    const A: u16 = 1 << 1;
    const S: u16 = 1 << 2;
    const E: u16 = 1 << 5;
    const ALT: u16 = 1 << 6;
    const SHIFT: u16 = 1 << 7;
    const BLOCKED_CTRL_META: u16 = 1 << 8;
    const SPACE: u16 = 1 << 9;

    fn ready() -> PilotContext {
        PilotContext {
            active: true,
            running: true,
            can_control: true,
            mission_running: true,
            modal: false,
        }
    }

    fn command(effect: PilotEffect) -> [f64; 3] {
        match effect {
            PilotEffect::SetCommand(command) => command,
            PilotEffect::None | PilotEffect::ReleaseCommand => panic!("expected a ship command"),
        }
    }

    #[test]
    fn debug_only_and_blocked_samples_cannot_take_an_unowned_command() {
        let mut pilot = KeyboardPilot::default();
        for physical in 0..64 {
            for modifier in [ALT, ALT | SHIFT, BLOCKED_CTRL_META, 1 << 15] {
                assert_eq!(
                    pilot.update(decode(physical | modifier), ready(), 10.0),
                    PilotEffect::None
                );
                assert!(!pilot.owns_command());
            }
        }
        assert_eq!(pilot.update(decode(0), ready(), 10.0), PilotEffect::None);
    }

    #[test]
    fn every_nonzero_ship_direction_is_normalized_and_bounded() {
        for max_accel in [0.1, 1.0, 9.81, f64::MIN_POSITIVE, 1e300, f64::MAX] {
            for physical in 1..64 {
                let movement = decode(physical);
                let mut pilot = KeyboardPilot::default();
                let effect = pilot.update(movement, ready(), max_accel);
                if movement.axes == [0.0; 3] {
                    assert_eq!(effect, PilotEffect::None);
                    assert!(!pilot.owns_command());
                    continue;
                }
                let command = command(effect);
                let length = magnitude(command);
                assert!(command.iter().all(|axis| axis.is_finite()));
                assert!(length > 0.0 && length <= max_accel);
                assert!((length / max_accel - 1.0).abs() < 1e-14);
                for (axis, input) in command.into_iter().zip(movement.axes) {
                    assert_eq!(axis.signum() * f64::from(u8::from(axis != 0.0)), input);
                }
                assert!(pilot.owns_command());
            }
        }
    }

    #[test]
    fn repeated_held_samples_set_the_command_without_camera_boost() {
        let mut pilot = KeyboardPilot::default();
        for movement in [
            decode(W),
            decode(W | SHIFT),
            Movement {
                boost: true,
                ..decode(W)
            },
            decode(W),
        ] {
            assert_eq!(
                pilot.update(movement, ready(), 10.0),
                PilotEffect::SetCommand([-10.0, 0.0, 0.0])
            );
            assert!(pilot.owns_command());
        }
    }

    #[test]
    fn owned_commands_release_once_for_key_up_zero_axes_and_modifier_handoffs() {
        for mask in [0, W | S, W | ALT, W | BLOCKED_CTRL_META, W | (1 << 15)] {
            let mut pilot = KeyboardPilot::default();
            command(pilot.update(decode(W), ready(), 10.0));
            assert_eq!(
                pilot.update(decode(mask), ready(), 10.0),
                PilotEffect::ReleaseCommand
            );
            assert!(!pilot.owns_command());
            assert_eq!(pilot.update(decode(mask), ready(), 10.0), PilotEffect::None);
        }
    }

    #[test]
    fn deliberate_alt_release_can_return_to_ship_control_with_w_still_held() {
        let mut pilot = KeyboardPilot::default();
        command(pilot.update(decode(W), ready(), 10.0));
        assert_eq!(
            pilot.update(decode(W | ALT | SHIFT), ready(), 10.0),
            PilotEffect::ReleaseCommand
        );
        assert_eq!(
            pilot.update(decode(W | ALT), ready(), 10.0),
            PilotEffect::None
        );
        assert_eq!(
            pilot.update(decode(W | SHIFT), ready(), 10.0),
            PilotEffect::SetCommand([-10.0, 0.0, 0.0])
        );
    }

    #[test]
    fn lost_authority_never_writes_and_regaining_it_requires_rearming() {
        for initially_owned in [false, true] {
            let mut pilot = KeyboardPilot::default();
            if initially_owned {
                command(pilot.update(decode(W), ready(), 10.0));
            }
            let unauthorized = PilotContext {
                can_control: false,
                ..ready()
            };
            for mask in [W, 0, W | S, W | ALT] {
                assert_eq!(
                    pilot.update(decode(mask), unauthorized, 10.0),
                    PilotEffect::None
                );
                assert!(!pilot.owns_command());
            }
            assert_eq!(
                pilot.update(decode(W), ready(), 10.0),
                if initially_owned {
                    PilotEffect::ReleaseCommand
                } else {
                    PilotEffect::None
                }
            );
            assert_eq!(pilot.update(decode(0), ready(), 10.0), PilotEffect::None);
            assert_eq!(
                pilot.update(decode(W), ready(), 10.0),
                PilotEffect::SetCommand([-10.0, 0.0, 0.0])
            );
        }
    }

    #[test]
    fn focus_pause_modal_and_mission_loss_release_then_require_physical_key_up() {
        for unavailable in [
            PilotContext {
                active: false,
                ..ready()
            },
            PilotContext {
                running: false,
                ..ready()
            },
            PilotContext {
                modal: true,
                ..ready()
            },
            PilotContext {
                mission_running: false,
                ..ready()
            },
        ] {
            let mut pilot = KeyboardPilot::default();
            command(pilot.update(decode(W), ready(), 10.0));
            assert_eq!(
                pilot.update(decode(W), unavailable, 10.0),
                PilotEffect::ReleaseCommand
            );
            assert!(!pilot.owns_command());
            for held in [W, W | S, W | ALT, W | BLOCKED_CTRL_META, W] {
                assert_eq!(pilot.update(decode(held), ready(), 10.0), PilotEffect::None);
            }
            assert_eq!(pilot.update(decode(0), ready(), 10.0), PilotEffect::None);
            assert_eq!(
                pilot.update(decode(W), ready(), 10.0),
                PilotEffect::SetCommand([-10.0, 0.0, 0.0])
            );
        }
    }

    #[test]
    fn focus_loss_with_a_cleared_sample_still_requires_an_active_release() {
        let mut pilot = KeyboardPilot::default();
        command(pilot.update(decode(W), ready(), 10.0));
        let inactive = PilotContext {
            active: false,
            ..ready()
        };
        assert_eq!(
            pilot.update(decode(0), inactive, 10.0),
            PilotEffect::ReleaseCommand
        );
        assert_eq!(pilot.update(decode(0), inactive, 10.0), PilotEffect::None);
        assert_eq!(pilot.update(decode(W), ready(), 10.0), PilotEffect::None);
        assert_eq!(pilot.update(decode(0), ready(), 10.0), PilotEffect::None);
        command(pilot.update(decode(W), ready(), 10.0));
    }

    #[test]
    fn initial_paused_held_keys_do_not_start_thrust_on_run() {
        let mut pilot = KeyboardPilot::default();
        let paused = PilotContext {
            running: false,
            ..ready()
        };
        assert_eq!(pilot.update(decode(W), paused, 10.0), PilotEffect::None);
        assert_eq!(pilot.update(decode(W), ready(), 10.0), PilotEffect::None);
        assert_eq!(pilot.update(decode(0), ready(), 10.0), PilotEffect::None);
        command(pilot.update(decode(W), ready(), 10.0));
    }

    #[test]
    fn deferred_authority_release_is_once_only_and_external_commands_can_supersede_it() {
        let unavailable = PilotContext {
            can_control: false,
            ..ready()
        };
        let mut pilot = KeyboardPilot::default();
        command(pilot.update(decode(W), ready(), 10.0));
        assert_eq!(
            pilot.update(decode(W), unavailable, 10.0),
            PilotEffect::None
        );
        assert!(!pilot.owns_command());
        assert!(pilot.has_installed_command());
        assert!(pilot.requires_release());
        assert_eq!(
            pilot.update(decode(W), ready(), 10.0),
            PilotEffect::ReleaseCommand
        );
        assert!(!pilot.has_installed_command());
        assert_eq!(pilot.update(decode(W), ready(), 10.0), PilotEffect::None);
        pilot.update(decode(0), ready(), 10.0);
        command(pilot.update(decode(W), ready(), 10.0));
        pilot.update(decode(W), unavailable, 10.0);
        pilot.cancel_until_release();
        assert!(!pilot.has_installed_command());
        assert_eq!(pilot.update(decode(W), ready(), 10.0), PilotEffect::None);
        assert_eq!(pilot.update(decode(0), ready(), 10.0), PilotEffect::None);
        command(pilot.update(decode(W), ready(), 10.0));
    }

    #[test]
    fn authority_return_releases_pending_thrust_but_only_active_key_up_rearms() {
        for context in [
            ready(),
            PilotContext {
                active: false,
                ..ready()
            },
            PilotContext {
                running: false,
                ..ready()
            },
            PilotContext {
                modal: true,
                ..ready()
            },
            PilotContext {
                mission_running: false,
                ..ready()
            },
        ] {
            let mut pilot = KeyboardPilot::default();
            command(pilot.update(decode(W), ready(), 10.0));
            pilot.update(
                decode(W),
                PilotContext {
                    can_control: false,
                    ..ready()
                },
                10.0,
            );
            assert_eq!(
                pilot.update(decode(0), context, 10.0),
                PilotEffect::ReleaseCommand
            );
            assert!(!pilot.has_installed_command());
            let rearmed = context.active && !context.modal;
            assert_eq!(pilot.requires_release(), !rearmed);
            assert_eq!(
                pilot.update(decode(W), ready(), 10.0),
                if rearmed {
                    PilotEffect::SetCommand([-10.0, 0.0, 0.0])
                } else {
                    PilotEffect::None
                }
            );
        }
    }

    #[test]
    fn external_cancellation_discards_ownership_without_releasing_external_commands() {
        for initially_owned in [false, true] {
            let mut pilot = KeyboardPilot::default();
            if initially_owned {
                command(pilot.update(decode(W), ready(), 10.0));
            }
            pilot.cancel_until_release();
            assert!(!pilot.owns_command());
            for mask in [W, W | S, W | ALT, W | BLOCKED_CTRL_META, W] {
                assert_eq!(pilot.update(decode(mask), ready(), 10.0), PilotEffect::None);
            }
            assert_eq!(pilot.update(decode(0), ready(), 10.0), PilotEffect::None);
            command(pilot.update(decode(W), ready(), 10.0));
        }
    }

    #[test]
    fn rebinding_releases_only_an_installed_keyboard_command_once() {
        for owned in [false, true] {
            let mut pilot = KeyboardPilot::default();
            if owned {
                command(pilot.update(decode(W), ready(), 10.0));
            }
            pilot.block_until_release();
            assert!(pilot.requires_release());
            assert_eq!(
                pilot.update(decode(W), ready(), 10.0),
                if owned {
                    PilotEffect::ReleaseCommand
                } else {
                    PilotEffect::None
                }
            );
            assert_eq!(pilot.update(decode(W), ready(), 10.0), PilotEffect::None);
            pilot.update(decode(0), ready(), 10.0);
            assert_eq!(
                pilot.update(decode(W), ready(), 10.0),
                PilotEffect::SetCommand([-10.0, 0.0, 0.0])
            );
        }
        let mut pilot = KeyboardPilot::default();
        command(pilot.update(decode(W), ready(), 10.0));
        pilot.block_until_release();
        pilot.cancel_until_release();
        assert_eq!(
            pilot.update(decode(0), ready(), 10.0),
            PilotEffect::None,
            "an external replacement discards the pending keyboard release"
        );
    }

    #[test]
    fn rearm_does_not_accept_releases_while_inactive_or_modal() {
        let mut pilot = KeyboardPilot::default();
        pilot.cancel_until_release();
        for unavailable in [
            PilotContext {
                active: false,
                ..ready()
            },
            PilotContext {
                modal: true,
                ..ready()
            },
        ] {
            assert_eq!(
                pilot.update(decode(0), unavailable, 10.0),
                PilotEffect::None
            );
            assert_eq!(pilot.update(decode(W), ready(), 10.0), PilotEffect::None);
        }
        let paused = PilotContext {
            running: false,
            ..ready()
        };
        assert_eq!(pilot.update(decode(0), paused, 10.0), PilotEffect::None);
        command(pilot.update(decode(W), ready(), 10.0));
    }

    #[test]
    fn fresh_launch_clears_ownership_inhibition_and_pending_command_release() {
        for context in [
            ready(),
            PilotContext {
                running: false,
                ..ready()
            },
            PilotContext {
                can_control: false,
                ..ready()
            },
        ] {
            let mut pilot = KeyboardPilot::default();
            command(pilot.update(decode(W), ready(), 10.0));
            pilot.update(decode(W), context, 10.0);
            pilot.rearm_for_launch();
            assert!(!pilot.has_installed_command());
            assert!(!pilot.requires_release());
            assert_eq!(
                pilot.update(decode(SPACE), ready(), 10.0),
                PilotEffect::SetCommand([0.0, 0.0, 10.0])
            );
        }
    }

    #[test]
    fn cancellation_cannot_emit_a_late_release_after_focus_or_pause_loss() {
        let mut pilot = KeyboardPilot::default();
        command(pilot.update(decode(W), ready(), 10.0));
        pilot.cancel_until_release();
        for context in [
            PilotContext {
                active: false,
                ..ready()
            },
            PilotContext {
                running: false,
                ..ready()
            },
            ready(),
        ] {
            assert_eq!(pilot.update(decode(W), context, 10.0), PilotEffect::None);
        }
    }

    #[test]
    fn invalid_limits_and_nonfinite_axes_never_produce_invalid_commands() {
        for invalid_limit in [0.0, -1.0, f64::NAN, f64::INFINITY, f64::NEG_INFINITY] {
            let mut pilot = KeyboardPilot::default();
            assert_eq!(
                pilot.update(decode(W), ready(), invalid_limit),
                PilotEffect::None
            );
            command(pilot.update(decode(W), ready(), 10.0));
            assert_eq!(
                pilot.update(decode(W), ready(), invalid_limit),
                PilotEffect::ReleaseCommand
            );
            assert!(!pilot.owns_command());
        }
        for invalid_axis in [f64::NAN, f64::INFINITY, f64::NEG_INFINITY] {
            let movement = Movement {
                axes: [invalid_axis, 1.0, 0.0],
                ..decode(W)
            };
            let mut pilot = KeyboardPilot::default();
            assert_eq!(pilot.update(movement, ready(), 10.0), PilotEffect::None);
            command(pilot.update(decode(W), ready(), 10.0));
            assert_eq!(
                pilot.update(movement, ready(), 10.0),
                PilotEffect::ReleaseCommand
            );
        }
    }

    #[test]
    fn updates_leave_movement_context_and_limit_inputs_unchanged() {
        let movement = decode(W | A | E | SHIFT);
        let context = ready();
        let max_accel = 10.0;
        let mut pilot = KeyboardPilot::default();
        let first = pilot.update(movement, context, max_accel);
        assert_eq!(pilot.update(movement, context, max_accel), first);
        assert_eq!(movement.target, MovementTarget::Ship);
        assert_eq!(movement.axes, [-1.0, -1.0, 1.0]);
        assert!(movement.held);
        assert!(!movement.boost);
        assert!(
            context.active && context.running && context.can_control && context.mission_running
        );
        assert!(!context.modal);
        assert_eq!(max_accel, 10.0);
    }
}
