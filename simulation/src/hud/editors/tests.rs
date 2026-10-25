//! Pure validation tests. Native text, focus and body-dispatch checks live in
//! hud::layout_tests and use the actual production widget tree.

use super::model::*;

#[test]
fn editor_focus_marker_is_zero_sized_and_blur_keeps_manual_input_blocked_until_release() {
    use bevy::prelude::{ButtonInput, KeyCode};
    crate::controls::reset_for_tests();
    assert_eq!(std::mem::size_of::<InstrumentEditor>(), 0);
    let mut keys = ButtonInput::default();
    super::systems::begin_input_frame(false, &keys);
    super::systems::begin_input_frame(false, &keys);
    assert!(!crate::hud::HudState::default().movement_blocked());
    super::systems::begin_input_frame(true, &keys);
    assert!(crate::hud::HudState::default().movement_blocked());
    // Native TextInput can clear InputFocus during its Escape handler. The
    // HUD bridge keeps that blur frame blocked even if no movement key is held.
    super::systems::begin_input_frame(false, &keys);
    assert!(crate::hud::HudState::default().movement_blocked());
    keys.press(KeyCode::KeyE);
    super::systems::begin_input_frame(false, &keys);
    assert!(crate::hud::HudState::default().movement_blocked());
    keys.release(KeyCode::KeyE);
    super::systems::begin_input_frame(false, &keys);
    assert!(!crate::hud::HudState::default().movement_blocked());
}

#[test]
fn counts_accept_finite_whole_numbers_including_scientific_notation() {
    for (text, count) in [
        ("0", 0.0),
        ("500", 500.0),
        ("10000", 10_000.0),
        ("100000", 100_000.0),
        ("1e5", 100_000.0),
    ] {
        assert_eq!(EditorField::WorldCount.parse(text).unwrap(), count);
    }
    for text in ["-1", "1.5", "100001", "NaN", "inf", "1e309", "", "five"] {
        assert!(EditorField::WorldCount.parse(text).is_err(), "{text}");
    }
}

#[test]
fn input_inventory_has_no_measured_output_setters() {
    let mut fields = Vec::new();
    fields.extend(EditorField::SHIP);
    fields.extend(EditorField::FLOW);
    fields.extend(EditorField::WORLD);
    fields.extend(EditorField::PLACEMENT);
    fields.extend(EditorField::IMPACT);
    for (index, field) in fields.iter().enumerate() {
        assert!(
            !fields[..index].contains(field),
            "each field has one native input: {field:?}"
        );
        assert!(!field.label().is_empty() && field.label().is_ascii());
        assert!(!field.hint().is_empty() && field.hint().is_ascii());
        assert!(field.step().is_finite() && field.step() > 0.0);
        for invalid in ["NaN", "inf", "-inf", "1e309"] {
            assert!(field.parse(invalid).is_err());
        }
        match field {
            EditorField::Mass
            | EditorField::InertiaFactor
            | EditorField::ThrustGain
            | EditorField::StreamSpeed
            | EditorField::StreamSpread
            | EditorField::WorldCount
            | EditorField::RadiusMin
            | EditorField::RadiusMax
            | EditorField::ShellInner
            | EditorField::ShellOuter
            | EditorField::Placement(_)
            | EditorField::ImpactSource(_)
            | EditorField::ImpactKick(_)
            | EditorField::ImpactTangentialSpeed
            | EditorField::ImpactPatchAngle => {}
        }
    }
    assert_eq!(fields.len(), 21);
    assert!(EditorField::InertiaFactor.hint().contains("not speed"));
}

#[test]
fn opening_world_inputs_loads_versioned_defaults_without_generating_a_world() {
    let snapshot = crate::snapshot();
    let before = serde_json::to_string(snapshot.as_ref()).unwrap();
    let draft = ConfirmedInputs::from(snapshot.as_ref()).world_draft();
    assert_eq!(
        draft.settings.debris.count,
        snapshot
            .experiment_settings
            .unwrap_or_default()
            .debris
            .count
    );
    assert_eq!(draft.preset, snapshot.flight_preset);
    assert!(std::rc::Rc::ptr_eq(&snapshot, &crate::snapshot()));
    assert_eq!(
        before,
        serde_json::to_string(crate::snapshot().as_ref()).unwrap()
    );
}
