//! Typed flight bindings. DOM-code parsing stays at the JSON/native input edges.
use bevy::prelude::{ButtonInput, KeyCode};
use serde::{Deserialize, Serialize};
use std::cell::RefCell;

#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
pub(crate) enum Action {
    Front,
    Back,
    Left,
    Right,
    Up,
    Down,
    Lift,
}

impl Action {
    pub(crate) const ALL: [Self; 7] = [
        Self::Front,
        Self::Back,
        Self::Left,
        Self::Right,
        Self::Up,
        Self::Down,
        Self::Lift,
    ];

    pub(crate) const fn bit(self) -> u16 {
        match self {
            Self::Front => 1 << 0,
            Self::Back => 1 << 2,
            Self::Left => 1 << 1,
            Self::Right => 1 << 3,
            Self::Up => 1 << 5,
            Self::Down => 1 << 4,
            Self::Lift => 1 << 9,
        }
    }

    pub(crate) const fn label(self) -> &'static str {
        match self {
            Self::Front => "Front",
            Self::Back => "Back",
            Self::Left => "Left",
            Self::Right => "Right",
            Self::Up => "Up",
            Self::Down => "Down",
            Self::Lift => "Lift",
        }
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize)]
enum PhysicalKey {
    KeyA,
    KeyB,
    KeyC,
    KeyD,
    KeyE,
    KeyG,
    KeyJ,
    KeyK,
    KeyN,
    KeyO,
    KeyQ,
    KeyS,
    KeyU,
    KeyV,
    KeyW,
    KeyY,
    KeyZ,
    Space,
}

impl PhysicalKey {
    fn parse(code: &str) -> Result<Self, String> {
        match code {
            "KeyA" => Ok(Self::KeyA),
            "KeyB" => Ok(Self::KeyB),
            "KeyC" => Ok(Self::KeyC),
            "KeyD" => Ok(Self::KeyD),
            "KeyE" => Ok(Self::KeyE),
            "KeyG" => Ok(Self::KeyG),
            "KeyJ" => Ok(Self::KeyJ),
            "KeyK" => Ok(Self::KeyK),
            "KeyN" => Ok(Self::KeyN),
            "KeyO" => Ok(Self::KeyO),
            "KeyQ" => Ok(Self::KeyQ),
            "KeyS" => Ok(Self::KeyS),
            "KeyU" => Ok(Self::KeyU),
            "KeyV" => Ok(Self::KeyV),
            "KeyW" => Ok(Self::KeyW),
            "KeyY" => Ok(Self::KeyY),
            "KeyZ" => Ok(Self::KeyZ),
            "Space" => Ok(Self::Space),
            _ => Err(format!(
                "{code} is reserved or unsupported. Choose Space or a letter other than F, H, I, L, M, P, R, T or X."
            )),
        }
    }

    fn from_native(key: KeyCode) -> Result<Self, String> {
        match key {
            KeyCode::KeyA => Ok(Self::KeyA),
            KeyCode::KeyB => Ok(Self::KeyB),
            KeyCode::KeyC => Ok(Self::KeyC),
            KeyCode::KeyD => Ok(Self::KeyD),
            KeyCode::KeyE => Ok(Self::KeyE),
            KeyCode::KeyG => Ok(Self::KeyG),
            KeyCode::KeyJ => Ok(Self::KeyJ),
            KeyCode::KeyK => Ok(Self::KeyK),
            KeyCode::KeyN => Ok(Self::KeyN),
            KeyCode::KeyO => Ok(Self::KeyO),
            KeyCode::KeyQ => Ok(Self::KeyQ),
            KeyCode::KeyS => Ok(Self::KeyS),
            KeyCode::KeyU => Ok(Self::KeyU),
            KeyCode::KeyV => Ok(Self::KeyV),
            KeyCode::KeyW => Ok(Self::KeyW),
            KeyCode::KeyY => Ok(Self::KeyY),
            KeyCode::KeyZ => Ok(Self::KeyZ),
            KeyCode::Space => Ok(Self::Space),
            _ => Err(format!(
                "{key:?} is reserved or unsupported. Choose Space or a letter other than F, H, I, L, M, P, R, T or X."
            )),
        }
    }

    const fn native(self) -> KeyCode {
        match self {
            Self::KeyA => KeyCode::KeyA,
            Self::KeyB => KeyCode::KeyB,
            Self::KeyC => KeyCode::KeyC,
            Self::KeyD => KeyCode::KeyD,
            Self::KeyE => KeyCode::KeyE,
            Self::KeyG => KeyCode::KeyG,
            Self::KeyJ => KeyCode::KeyJ,
            Self::KeyK => KeyCode::KeyK,
            Self::KeyN => KeyCode::KeyN,
            Self::KeyO => KeyCode::KeyO,
            Self::KeyQ => KeyCode::KeyQ,
            Self::KeyS => KeyCode::KeyS,
            Self::KeyU => KeyCode::KeyU,
            Self::KeyV => KeyCode::KeyV,
            Self::KeyW => KeyCode::KeyW,
            Self::KeyY => KeyCode::KeyY,
            Self::KeyZ => KeyCode::KeyZ,
            Self::Space => KeyCode::Space,
        }
    }

    const fn label(self) -> &'static str {
        match self {
            Self::KeyA => "A",
            Self::KeyB => "B",
            Self::KeyC => "C",
            Self::KeyD => "D",
            Self::KeyE => "E",
            Self::KeyG => "G",
            Self::KeyJ => "J",
            Self::KeyK => "K",
            Self::KeyN => "N",
            Self::KeyO => "O",
            Self::KeyQ => "Q",
            Self::KeyS => "S",
            Self::KeyU => "U",
            Self::KeyV => "V",
            Self::KeyW => "W",
            Self::KeyY => "Y",
            Self::KeyZ => "Z",
            Self::Space => "Space",
        }
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct ActionMap<T> {
    front: T,
    back: T,
    left: T,
    right: T,
    up: T,
    down: T,
    lift: T,
}

impl<T> ActionMap<T> {
    fn get(&self, action: Action) -> &T {
        match action {
            Action::Front => &self.front,
            Action::Back => &self.back,
            Action::Left => &self.left,
            Action::Right => &self.right,
            Action::Up => &self.up,
            Action::Down => &self.down,
            Action::Lift => &self.lift,
        }
    }

    fn get_mut(&mut self, action: Action) -> &mut T {
        match action {
            Action::Front => &mut self.front,
            Action::Back => &mut self.back,
            Action::Left => &mut self.left,
            Action::Right => &mut self.right,
            Action::Up => &mut self.up,
            Action::Down => &mut self.down,
            Action::Lift => &mut self.lift,
        }
    }
}

type Bindings = ActionMap<PhysicalKey>;

impl Default for Bindings {
    fn default() -> Self {
        Self {
            front: PhysicalKey::KeyW,
            back: PhysicalKey::KeyS,
            left: PhysicalKey::KeyA,
            right: PhysicalKey::KeyD,
            up: PhysicalKey::KeyE,
            down: PhysicalKey::KeyQ,
            lift: PhysicalKey::Space,
        }
    }
}

impl Bindings {
    fn validate(self) -> Result<Self, String> {
        for (index, action) in Action::ALL.into_iter().enumerate() {
            for other in &Action::ALL[..index] {
                if self.get(action) == self.get(*other) {
                    return Err(format!(
                        "{} already controls {}. Choose another key for {}.",
                        self.get(action).label(),
                        other.label(),
                        action.label()
                    ));
                }
            }
        }
        Ok(self)
    }
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct BindingRequest {
    version: u32,
    bindings: ActionMap<String>,
}

#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
enum BindingPhase {
    #[default]
    Ready,
    Capturing(Action),
    AwaitingRelease,
}

impl BindingPhase {
    fn capture_action(self) -> Option<Action> {
        match self {
            Self::Capturing(action) => Some(action),
            Self::Ready | Self::AwaitingRelease => None,
        }
    }

    fn requires_release(self) -> bool {
        match self {
            Self::Ready => false,
            Self::Capturing(_) | Self::AwaitingRelease => true,
        }
    }
}

#[derive(Default)]
struct BindingState {
    bindings: Bindings,
    revision: u64,
    phase: BindingPhase,
    message: String,
    watched_keys: Vec<KeyCode>,
    // Consuming the last capture or release event must also block the rest of
    // its frame, even when the phase has already returned to Ready.
    frame_blocks_actions: bool,
}

impl BindingState {
    fn require_release(&mut self) {
        self.phase = BindingPhase::AwaitingRelease;
        self.frame_blocks_actions = true;
        for action in Action::ALL {
            self.watch(self.bindings.get(action).native());
        }
    }

    fn watch(&mut self, key: KeyCode) {
        if !self.watched_keys.contains(&key) {
            self.watched_keys.push(key);
        }
    }
}

thread_local! {
    static STATE: RefCell<BindingState> = RefCell::new(BindingState::default());
}

pub(super) fn status() -> String {
    STATE.with(|state| {
        let state = state.borrow();
        let capture = state.phase.capture_action();
        serde_json::json!({
            "version": 1,
            "revision": state.revision,
            "bindings": state.bindings,
            "action_bits": ActionMap {
                front: Action::Front.bit(), back: Action::Back.bit(),
                left: Action::Left.bit(), right: Action::Right.bit(),
                up: Action::Up.bit(), down: Action::Down.bit(), lift: Action::Lift.bit(),
            },
            "modifier_bits": {"alt": super::routing::ALT, "shift": super::routing::SHIFT, "blocked": super::routing::BLOCKED_CTRL_META},
            "capture": {"active": capture.is_some(), "action": capture, "message": state.message},
            "requires_release": state.phase.requires_release(),
        }).to_string()
    })
}

pub(super) fn set(json: &str) -> Result<(), String> {
    if json.len() > 4096 {
        return Err("Binding settings are too large.".into());
    }
    let request: BindingRequest =
        serde_json::from_str(json).map_err(|error| format!("Invalid binding settings: {error}"))?;
    if request.version != 1 {
        return Err("Unsupported binding version. Use version 1.".into());
    }
    let mut next = Bindings::default();
    for action in Action::ALL {
        *next.get_mut(action) = PhysicalKey::parse(request.bindings.get(action))?;
    }
    replace(
        next.validate()?,
        "Bindings saved. Release the movement keys before flying again.".into(),
    );
    Ok(())
}

fn replace(next: Bindings, message: String) {
    STATE.with(|state| {
        let mut state = state.borrow_mut();
        // Keep both watches: a held key removed from the map still needs its
        // real key-up, just as an already-held new binding does.
        state.require_release();
        state.bindings = next;
        state.require_release();
        state.revision = state.revision.saturating_add(1);
        state.message = message;
    });
    super::routing::set_movement_keys(0);
    crate::session::block_keyboard_until_release();
}

pub(crate) fn restore_defaults() {
    replace(
        Bindings::default(),
        "Default bindings restored. Release the movement keys before flying again.".into(),
    );
}

pub(crate) fn begin_capture(action: Action) {
    STATE.with(|state| {
        let mut state = state.borrow_mut();
        state.require_release();
        state.phase = BindingPhase::Capturing(action);
        state.message = format!("{}: press one physical key. Esc cancels. Movement is blocked while changing a binding.", action.label());
    });
    super::routing::set_movement_keys(0);
    crate::session::block_keyboard_until_release();
}

pub(crate) fn cancel_capture() {
    STATE.with(|state| {
        let mut state = state.borrow_mut();
        match state.phase {
            BindingPhase::Capturing(_) => {
                state.require_release();
                state.message =
                    "Key change cancelled. Release the movement keys before flying again.".into();
            }
            BindingPhase::Ready | BindingPhase::AwaitingRelease => {}
        }
    });
}

pub(crate) fn capture_active() -> bool {
    STATE.with(|state| state.borrow().phase.capture_action().is_some())
}

pub(crate) fn actions_blocked() -> bool {
    STATE.with(|state| {
        let state = state.borrow();
        state.phase.requires_release() || state.frame_blocks_actions
    })
}

pub(crate) fn manual_input_blocked() -> bool {
    STATE.with(|state| state.borrow().phase.requires_release())
}

pub(crate) fn begin_input_frame() {
    STATE.with(|state| {
        let mut state = state.borrow_mut();
        state.frame_blocks_actions = state.phase.requires_release();
    });
}

pub(crate) fn capture_key(key: KeyCode, modified: bool) {
    let next = STATE.with(|state| {
        let mut state = state.borrow_mut();
        let action = state.phase.capture_action()?;
        state.frame_blocks_actions = true;
        state.watch(key);
        if key == KeyCode::Escape {
            drop(state);
            cancel_capture();
            return None;
        }
        let key = if modified {
            Err("Use one key without Alt/Option, Shift, Ctrl or Meta. Esc cancels.".into())
        } else {
            PhysicalKey::from_native(key)
        };
        let result = key.and_then(|key| {
            if let Some(other) = Action::ALL
                .into_iter()
                .find(|other| *other != action && *state.bindings.get(*other) == key)
            {
                return Err(format!(
                    "{} already controls {}. Choose another key for {}.",
                    key.label(),
                    other.label(),
                    action.label()
                ));
            }
            let mut next = state.bindings;
            *next.get_mut(action) = key;
            next.validate()
        });
        match result {
            Ok(next) => Some((next, action)),
            Err(error) => {
                state.message = error;
                None
            }
        }
    });
    if let Some((next, action)) = next {
        let message = format!(
            "{} set to {}. Release the movement keys before flying again.",
            action.label(),
            next.get(action).label()
        );
        replace(next, message);
    }
}

pub(crate) fn key_label(action: Action) -> &'static str {
    STATE.with(|state| state.borrow().bindings.get(action).label())
}

pub(crate) fn capture_message() -> String {
    STATE.with(|state| {
        let state = state.borrow();
        if state.message.is_empty() {
            "Choose a binding to change. One physical key per action; no chords.".into()
        } else {
            state.message.clone()
        }
    })
}

#[cfg(any(not(target_arch = "wasm32"), test))]
pub(super) fn native_mask(keys: &ButtonInput<KeyCode>) -> u16 {
    STATE.with(|state| {
        let state = state.borrow();
        Action::ALL.into_iter().fold(0, |mask, action| {
            mask | if keys.pressed(state.bindings.get(action).native()) {
                action.bit()
            } else {
                0
            }
        })
    })
}

/// Observe physical key-up, including keys removed from the binding map. Clearing
/// the browser's abstract mask alone must not rearm a native held key.
pub(super) fn observe_release(keys: &ButtonInput<KeyCode>, mask: u16) -> bool {
    STATE.with(|state| {
        let mut state = state.borrow_mut();
        let held = state.watched_keys.iter().any(|key| keys.pressed(*key))
            || mask & super::routing::TRANSLATION_KEYS != 0;
        if state.phase == BindingPhase::AwaitingRelease && !held && crate::camera::input_active() {
            state.phase = BindingPhase::Ready;
            state.frame_blocks_actions = true;
            state.watched_keys.clear();
        }
        held
    })
}

/// UI input must observe release even without an installed physics runtime.
/// Run after capture handling and before native widgets dispatch this frame.
pub(crate) fn observe_input_release(keys: &ButtonInput<KeyCode>) {
    observe_release(keys, super::routing::key_mask(keys));
}

#[cfg(test)]
pub(crate) fn reset_for_tests() {
    STATE.with(|state| *state.borrow_mut() = BindingState::default());
    super::routing::set_movement_keys(0);
}

#[cfg(test)]
mod tests {
    use super::*;

    const DEFAULTS: &str = r#"{"version":1,"bindings":{"front":"KeyW","back":"KeyS","left":"KeyA","right":"KeyD","up":"KeyE","down":"KeyQ","lift":"Space"}}"#;

    #[test]
    fn status_reports_the_typed_default_map_and_abstract_bits() {
        reset_for_tests();
        let report: serde_json::Value = serde_json::from_str(&status()).unwrap();
        assert_eq!(
            report["bindings"],
            serde_json::from_str::<serde_json::Value>(DEFAULTS).unwrap()["bindings"]
        );
        assert_eq!(
            report["action_bits"],
            serde_json::json!({"front":1,"back":4,"left":2,"right":8,"up":32,"down":16,"lift":512})
        );
        assert_eq!(report["capture"]["active"], false);
        assert_eq!(report["capture"]["action"], serde_json::Value::Null);
        assert_eq!(report["requires_release"], false);
        assert_eq!(report["revision"], 0);
    }

    #[test]
    fn invalid_json_reserved_keys_duplicates_and_unknown_fields_are_atomic() {
        reset_for_tests();
        let before = status();
        for invalid in [
            "{}",
            "[]",
            "not JSON",
            &DEFAULTS.replace("\"version\":1", "\"version\":2"),
            &DEFAULTS.replace("\"KeyW\"", "\"KeyS\""),
            &DEFAULTS.replace("\"version\":1", "\"version\":1,\"extra\":true"),
        ] {
            assert!(set(invalid).is_err(), "{invalid}");
            assert_eq!(status(), before);
        }
        for code in [
            "KeyH",
            "KeyP",
            "KeyF",
            "KeyI",
            "KeyL",
            "KeyR",
            "KeyT",
            "KeyM",
            "KeyX",
            "Tab",
            "Enter",
            "Escape",
            "Digit1",
            "Digit7",
            "ControlLeft",
            "AltRight",
            "ShiftLeft",
            "SuperRight",
            "ArrowUp",
            "KeyAA",
            "w",
        ] {
            assert!(
                set(&DEFAULTS.replace("KeyW", code))
                    .unwrap_err()
                    .contains("reserved")
            );
            assert_eq!(status(), before);
        }
    }

    #[test]
    fn capture_rejects_reserved_and_duplicate_keys_then_saves_or_cancels() {
        reset_for_tests();
        begin_capture(Action::Front);
        capture_key(KeyCode::KeyH, false);
        assert!(capture_active());
        assert!(capture_message().contains("reserved"));
        capture_key(KeyCode::KeyA, false);
        assert!(capture_active());
        assert!(capture_message().contains("already controls Left"));
        capture_key(KeyCode::KeyB, true);
        assert!(capture_message().contains("without Alt/Option"));
        capture_key(KeyCode::KeyB, false);
        assert!(!capture_active());
        assert_eq!(key_label(Action::Front), "B");
        assert!(manual_input_blocked());
        assert!(actions_blocked());
        begin_capture(Action::Back);
        capture_key(KeyCode::Escape, false);
        assert!(!capture_active());
        assert_eq!(key_label(Action::Back), "S");
        reset_for_tests();
    }

    #[test]
    fn configured_native_codes_keep_old_and_new_keys_in_the_release_guard() {
        reset_for_tests();
        crate::camera::set_input_active(true, false);
        let mut keys = ButtonInput::default();
        keys.press(KeyCode::KeyW);
        set(&DEFAULTS.replace("KeyW", "KeyB")).unwrap();
        assert_eq!(native_mask(&keys), 0);
        assert!(observe_release(&keys, 0));
        assert!(manual_input_blocked());
        keys.press(KeyCode::KeyB);
        keys.release(KeyCode::KeyW);
        assert_eq!(native_mask(&keys), Action::Front.bit());
        assert!(observe_release(&keys, 0));
        keys.release(KeyCode::KeyB);
        assert!(!observe_release(&keys, 0));
        assert!(!manual_input_blocked());
        assert!(
            actions_blocked(),
            "the final key-up still belongs to the blocked frame"
        );
        begin_input_frame();
        assert!(!actions_blocked());
        assert!(keys.just_released(KeyCode::KeyW));
        reset_for_tests();
    }

    #[test]
    fn capture_and_release_are_distinct_phases_with_a_separate_frame_guard() {
        reset_for_tests();
        crate::camera::set_input_active(true, false);
        let mut keys = ButtonInput::default();
        begin_capture(Action::Lift);
        observe_input_release(&keys);
        assert!(
            capture_active(),
            "an empty input sample must not finish capture"
        );
        let capturing: serde_json::Value = serde_json::from_str(&status()).unwrap();
        assert_eq!(capturing["capture"]["action"], "lift");
        assert_eq!(capturing["requires_release"], true);

        keys.press(KeyCode::Space);
        begin_input_frame();
        capture_key(KeyCode::Space, false);
        observe_input_release(&keys);
        assert!(!capture_active());
        assert!(manual_input_blocked());
        begin_input_frame();
        observe_input_release(&keys);
        assert!(actions_blocked(), "a held captured Space must stay guarded");

        keys.release(KeyCode::Space);
        begin_input_frame();
        observe_input_release(&keys);
        assert!(!manual_input_blocked());
        assert!(
            actions_blocked(),
            "Space key-up cannot reactivate a binding button"
        );
        assert!(keys.just_released(KeyCode::Space));
        let released: serde_json::Value = serde_json::from_str(&status()).unwrap();
        assert_eq!(released["requires_release"], false);
        assert_eq!(released["capture"]["action"], serde_json::Value::Null);
        begin_input_frame();
        observe_input_release(&keys);
        assert!(
            !actions_blocked(),
            "the next frame must reenable native controls"
        );
        reset_for_tests();
    }

    #[test]
    fn replacement_keeps_removed_space_and_intermediate_bindings_watched() {
        reset_for_tests();
        crate::camera::set_input_active(true, false);
        let mut keys = ButtonInput::default();
        keys.press(KeyCode::Space);
        keys.press(KeyCode::KeyB);
        set(&DEFAULTS.replace("Space", "KeyB")).unwrap();
        restore_defaults();
        keys.release(KeyCode::Space);
        assert_eq!(native_mask(&keys), 0, "B was removed from the restored map");
        observe_input_release(&keys);
        assert!(
            manual_input_blocked(),
            "the intermediate binding still needs key-up"
        );
        keys.press(KeyCode::Space);
        keys.release(KeyCode::KeyB);
        observe_input_release(&keys);
        assert!(manual_input_blocked(), "the original Space is still held");
        keys.release(KeyCode::Space);
        observe_input_release(&keys);
        assert!(!manual_input_blocked());
        reset_for_tests();
    }

    #[test]
    fn cancellation_waits_for_rejected_keys_and_active_input_before_rearming() {
        reset_for_tests();
        crate::camera::set_input_active(true, false);
        let mut keys = ButtonInput::default();
        begin_capture(Action::Front);
        keys.press(KeyCode::KeyH);
        capture_key(KeyCode::KeyH, false);
        keys.press(KeyCode::Escape);
        capture_key(KeyCode::Escape, false);
        keys.release(KeyCode::Escape);
        observe_input_release(&keys);
        assert!(
            manual_input_blocked(),
            "rejected H is watched even outside the binding map"
        );
        keys.release(KeyCode::KeyH);
        crate::camera::set_input_active(false, false);
        observe_input_release(&keys);
        assert!(
            manual_input_blocked(),
            "a focus-loss clear is not a physical rearm"
        );
        crate::camera::set_input_active(true, false);
        observe_input_release(&keys);
        assert!(!manual_input_blocked());
        assert!(actions_blocked());
        begin_input_frame();
        assert!(!actions_blocked());
        reset_for_tests();
    }

    #[test]
    fn captured_shortcuts_stay_consumed_when_capture_and_release_share_a_frame() {
        crate::camera::set_input_active(true, false);
        for key in [KeyCode::KeyP, KeyCode::KeyH, KeyCode::KeyR, KeyCode::KeyF] {
            reset_for_tests();
            let mut keys = ButtonInput::default();
            begin_capture(Action::Front);
            begin_input_frame();
            keys.press(key);
            capture_key(key, false);
            keys.release(key);
            keys.press(KeyCode::Escape);
            capture_key(KeyCode::Escape, false);
            keys.release(KeyCode::Escape);
            observe_input_release(&keys);
            assert!(!manual_input_blocked());
            assert!(
                actions_blocked(),
                "{key:?} was consumed even though physical release is complete"
            );
            assert!(keys.just_pressed(key));
            begin_input_frame();
            assert!(!actions_blocked());
        }
        reset_for_tests();
    }
}
