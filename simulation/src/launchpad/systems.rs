use super::{
    BRIDGE, LaunchpadAction, LaunchpadSystems, SurfaceStep,
    geometry::{self, Grip},
    model::{Commit, Phase, Placement, Pointer, Source},
};
use crate::{
    EngineSet, PlaybackState,
    camera::{CameraRig, SimulationCamera},
    domain::{CameraMode, CameraRequest, EntityId},
};
use bevy::{
    camera::RenderTarget,
    ecs::system::SystemParam,
    input_focus::InputFocus,
    math::{DQuat, DVec3},
    picking::{
        Pickable,
        pointer::{
            PointerAction, PointerButton, PointerId, PointerInput, PointerLocation,
            PointerPressState,
        },
    },
    prelude::*,
    text::EditableText,
    ui_widgets::{Button as UiButton, ScrollArea, TextInput},
    window::PrimaryWindow,
};

pub(super) fn install(app: &mut App) {
    app.init_resource::<Placement>()
        .add_message::<PointerInput>()
        .add_systems(
            Update,
            update
                .in_set(EngineSet::Input)
                .in_set(LaunchpadSystems::Input)
                .before(crate::session::advance_world),
        )
        .add_systems(Update, draw.in_set(EngineSet::Scene));
}

type UiGeometry = (
    &'static Node,
    &'static ComputedNode,
    &'static UiGlobalTransform,
    &'static InheritedVisibility,
    &'static ComputedUiTargetCamera,
    Option<&'static CalculatedClip>,
    Option<&'static Pickable>,
);
type UiBlocker = Or<(
    With<BackgroundColor>,
    With<UiButton>,
    With<ScrollArea>,
    With<TextInput>,
)>;
type TextEditor = Or<(
    With<crate::hud::InstrumentEditor>,
    With<TextInput>,
    With<EditableText>,
)>;
type PlacementCamera = (
    Entity,
    &'static Camera,
    &'static GlobalTransform,
    &'static RenderTarget,
    &'static Transform,
    &'static Projection,
);

#[derive(SystemParam)]
struct Input<'w, 's> {
    keys: Res<'w, ButtonInput<KeyCode>>,
    buttons: Res<'w, ButtonInput<MouseButton>>,
    focus: Res<'w, InputFocus>,
    hud: Res<'w, crate::hud::HudState>,
    visuals: Res<'w, crate::scene::VisualSettings>,
    playback: ResMut<'w, PlaybackState>,
    rig: Res<'w, CameraRig>,
    windows: Query<'w, 's, (Entity, &'static Window), With<PrimaryWindow>>,
    cameras: Query<'w, 's, PlacementCamera, With<SimulationCamera>>,
    ui: Query<'w, 's, UiGeometry, UiBlocker>,
    editors: Query<'w, 's, (), TextEditor>,
    ui_buttons: Query<'w, 's, (), With<UiButton>>,
    pointers: Query<
        'w,
        's,
        (
            &'static PointerId,
            &'static PointerLocation,
            &'static PointerPressState,
        ),
    >,
    events: MessageReader<'w, 's, PointerInput>,
}

fn pointer(id: PointerId) -> Option<Pointer> {
    match id {
        PointerId::Mouse => Some(Pointer::Mouse),
        PointerId::Touch(id) => Some(Pointer::Touch(id)),
        PointerId::Custom(_) => None,
    }
}

fn over_ui(
    ui: &Query<UiGeometry, UiBlocker>,
    camera_entity: Entity,
    camera: &Camera,
    cursor: Vec2,
) -> bool {
    let Some(viewport) = camera.physical_viewport_rect() else {
        return true;
    };
    let Some(scale) = camera
        .target_scaling_factor()
        .filter(|scale| scale.is_finite() && *scale > 0.0)
    else {
        return true;
    };
    let point = cursor * scale - viewport.min.as_vec2();
    ui.iter().any(
        |(style, node, transform, visibility, target, clip, pickable)| {
            target.get() == Some(camera_entity)
                && style.display != Display::None
                && visibility.get()
                && !node.is_empty()
                && node.size().is_finite()
                && transform.affine().is_finite()
                && transform.try_inverse().is_some()
                && pickable.is_none_or(|pickable| pickable.should_block_lower)
                && node.contains_point(*transform, point)
                && clip.is_none_or(|clip| clip.contains_point(point))
        },
    )
}

fn interaction_blocked_reason(input: &Input, window: Option<&Window>) -> Option<&'static str> {
    if !input.playback.can_control {
        Some("Launchpad placement needs control. Camera controls still work in view-only mode.")
    } else if input.hud.launchpad_input_blocked()
        || input
            .focus
            .get()
            .is_some_and(|entity| input.editors.contains(entity))
    {
        Some("Placement cancelled for another editor or dialog. Close it, then try again.")
    } else if !crate::camera::input_active() || window.is_none_or(|window| !window.focused) {
        Some("Placement cancelled because the scene lost focus. Click the scene to try again.")
    } else {
        None
    }
}

fn flight_blocked_reason(input: &Input, window: Option<&Window>) -> Option<&'static str> {
    if input.playback.running || crate::session::is_running() {
        Some("Use Place lander to pause and frame the pad, or pause before dragging it directly.")
    } else {
        interaction_blocked_reason(input, window)
    }
}

const PREPARING: &str = "Preparing the Phobos view. Pad and lander are unchanged; Esc cancels.";

fn blocked_reason(
    input: &Input,
    window: Option<&Window>,
    ready: bool,
    preparing: bool,
) -> Option<&'static str> {
    if let Some(reason) = flight_blocked_reason(input, window) {
        Some(reason)
    } else if preparing {
        Some(PREPARING)
    } else if input.rig.mode.sensor().is_some() {
        Some("Use Place lander to open a debug placement view. Mounted RGB cameras stay unchanged.")
    } else if !ready {
        Some("Viewer projection is unavailable. Try again when the scene appears.")
    } else {
        None
    }
}

fn source(snapshot: &crate::model::Snapshot, state: &Placement) -> Option<Source> {
    Source::capture(
        snapshot,
        state.indices,
        crate::session::trial_revision(),
        crate::session::is_running(),
    )
}

fn commit(state: &mut Placement, operation: Commit) {
    // Only this checked adapter writes the pad and lander poses. A stale context
    // or rejected collision leaves physics alone; preparation only pauses playback.
    state.notice = Some(match crate::session::dispatch_launchpad_checked(operation.direction, operation.expected) {
        Ok(_) => "Pad and lander moved at rest. Local intervention recorded; flight target unchanged.".into(),
        Err(crate::api::CommandError::StaleContext) =>
            "Placement cancelled because the flight changed. Choose the pad again to retry.".into(),
        Err(crate::api::CommandError::Rejected(error)) => format!("Pad unchanged. {error}"),
    });
}

fn plain_keys(keys: &ButtonInput<KeyCode>) -> bool {
    !keys.any_pressed([
        KeyCode::ControlLeft,
        KeyCode::ControlRight,
        KeyCode::SuperLeft,
        KeyCode::SuperRight,
        KeyCode::AltLeft,
        KeyCode::AltRight,
    ])
}

fn update(mut input: Input, mut state: ResMut<Placement>) {
    let events: Vec<_> = input.events.read().cloned().collect();
    let held = input.keys.get_pressed().next().is_some()
        || input.buttons.get_pressed().next().is_some()
        || crate::controls::movement(&input.keys).held
        || input
            .pointers
            .iter()
            .any(|(id, _, press)| id.is_touch() && press.is_any_pressed());
    let released = input.keys.get_just_released().next().is_some()
        || input.buttons.get_just_released().next().is_some()
        || events.iter().any(|event| {
            matches!(
                event.action,
                PointerAction::Release(_) | PointerAction::Cancel
            )
        });
    state.begin_frame(held, released);
    let snapshot = crate::snapshot();
    state
        .indices
        .refresh(&snapshot, crate::session::trial_revision());
    let window = input.windows.single().ok();
    let view = input.cameras.single().ok();
    let ready = view.is_some_and(|(_, camera, global, target, transform, projection)| {
        let Some((window_entity, _)) = window else { return false; };
        let Some(rect) = camera.logical_viewport_rect() else { return false; };
        !rect.is_empty()
            && matches!(target.normalize(Some(window_entity)),
                Some(bevy::camera::NormalizedRenderTarget::Window(window)) if window.entity() == window_entity)
            && global.to_matrix().abs_diff_eq(transform.to_matrix(), 1.0e-4)
            && camera.computed.clip_from_view.abs_diff_eq(projection.get_clip_from_view(), 1.0e-5)
            && geometry::viewport_ray(camera, global, rect.center()).is_some()
    });
    let window_ref = window.map(|(_, window)| window);

    // This runs no earlier than the input frame after the requested real Pause
    // and queued camera view. Do not mistake the previous sensor/projection for
    // a failed placement, or retain a command legitimately released by Pause.
    if state.preparing() {
        if let Some(reason) = flight_blocked_reason(&input, window_ref) {
            state.cancel(reason);
        } else if !state.rebase_preparation(
            &snapshot,
            crate::session::trial_revision(),
            crate::session::is_running(),
        ) {
            state.cancel(
                "Placement cancelled because the flight changed. Choose the pad again to retry.",
            );
        } else if ready
            && input.rig.mode == CameraMode::Orbit
            && input.rig.focus == EntityId::Phobos
            && state
                .edit()
                .and_then(|edit| edit.preview)
                .is_some_and(|preview| {
                    view.is_some_and(|(_, camera, global, ..)| {
                        geometry::grip(camera, global, preview.site).is_some()
                    })
                })
        {
            state.finish_preparation();
        }
    }
    let reason = blocked_reason(&input, window_ref, ready, state.preparing());
    if state.active() && !state.preparing() {
        if let Some(reason) = reason {
            state.cancel(reason);
        } else if state.edit().is_some_and(|edit| {
            !edit.source.matches(
                &snapshot,
                crate::session::trial_revision(),
                crate::session::is_running(),
            )
        }) {
            state.cancel(
                "Placement cancelled because the flight changed. Choose the pad again to retry.",
            );
        } else if state.drag_pointer() == Some(Pointer::Mouse)
            && window.is_none_or(|(_, window)| window.cursor_position().is_none())
        {
            state.cancel(
                "Placement cancelled because the pointer left the scene. The pad is unchanged.",
            );
        }
    }

    let actions: Vec<_> = BRIDGE.with(|bridge| bridge.borrow_mut().actions.drain(..).collect());
    let text_focused = input
        .focus
        .get()
        .is_some_and(|entity| input.editors.contains(entity));
    let escape = !text_focused
        && !input.hud.launchpad_input_blocked()
        && input.keys.just_pressed(KeyCode::Escape)
        && plain_keys(&input.keys)
        && (state.active()
            || actions
                .iter()
                .any(|action| *action != LaunchpadAction::FramePhobos));
    if escape {
        state.cancel("Placement cancelled. Pad and lander are unchanged.");
    }
    for action in actions {
        if escape && action != LaunchpadAction::FramePhobos {
            continue;
        }
        let reason = blocked_reason(&input, window_ref, ready, state.preparing());
        match action {
            LaunchpadAction::Cancel => {
                state.cancel("Placement cancelled. Pad and lander are unchanged.")
            }
            LaunchpadAction::FramePhobos => {
                if let Err(error) = crate::camera::request_view(CameraRequest {
                    mode: Some(CameraMode::Orbit),
                    focus: Some(EntityId::Phobos),
                    zoom: Some(1.0),
                }) {
                    state.notice = Some(error.to_string());
                }
            }
            LaunchpadAction::PlaceOnSurface => {
                if let Some(reason) = interaction_blocked_reason(&input, window_ref) {
                    state.notice = Some(reason.into());
                } else if !state.blocked() {
                    let Some(initial) = geometry::actual_preview(&snapshot, state.indices) else {
                        state.notice = Some(
                            "The actual launchpad is unavailable. Reload a valid flight archive."
                                .into(),
                        );
                        continue;
                    };
                    let release_command = crate::session::keyboard_owns_command();
                    match crate::session::pause_for_launchpad_placement() {
                        Ok(paused) => {
                            *input.playback = paused;
                            if let Some(source) = source(&snapshot, &state) {
                                state.prepare(source, initial, release_command);
                                if let Err(error) = crate::camera::request_view(CameraRequest {
                                    mode: Some(CameraMode::Orbit),
                                    focus: Some(EntityId::Phobos),
                                    zoom: Some(1.0),
                                }) {
                                    state.cancel(format!("Placement cancelled. {error}"));
                                }
                            } else {
                                state.notice = Some(
                                    "The actual pad or lander is unavailable. Placement did not start."
                                        .into(),
                                );
                            }
                        }
                        Err(error) => state.notice = Some(format!("Pad unchanged. {error}")),
                    }
                }
            }
            LaunchpadAction::Nudge(step) => {
                if let Some(reason) = reason {
                    state.notice = Some(reason.into());
                } else {
                    nudge(&snapshot, &mut state, step);
                }
            }
            LaunchpadAction::Confirm => {
                if let Some(reason) = reason {
                    state.notice = Some(reason.into());
                } else if matches!(state.phase, Phase::Armed(_))
                    && let Some(operation) = state.take_commit()
                {
                    commit(&mut state, operation);
                }
            }
        }
    }

    let reason = blocked_reason(&input, window_ref, ready, state.preparing());
    if reason.is_none()
        && !text_focused
        && plain_keys(&input.keys)
        && matches!(state.phase, Phase::Armed(_))
    {
        for (key, step) in [
            (KeyCode::ArrowUp, SurfaceStep::North),
            (KeyCode::ArrowDown, SurfaceStep::South),
            (KeyCode::ArrowRight, SurfaceStep::East),
            (KeyCode::ArrowLeft, SurfaceStep::West),
        ] {
            if input.keys.just_pressed(key) {
                nudge(&snapshot, &mut state, step);
            }
        }
        let button_focused = input
            .focus
            .get()
            .is_some_and(|entity| input.ui_buttons.contains(entity));
        if !button_focused
            && input.keys.just_pressed(KeyCode::Enter)
            && let Some(operation) = state.take_commit()
        {
            commit(&mut state, operation);
        }
    }

    if let (Some((window_entity, _)), Some((camera_entity, camera, global, target, ..))) =
        (window, view)
    {
        let target = target.normalize(Some(window_entity));
        // Coalesce pointer movement between press/release events. Terrain work is
        // bounded by active pointers per frame, not native mouse event frequency.
        let mut pending_move = None;
        for event in &events {
            let Some(owner) = pointer(event.pointer_id) else {
                continue;
            };
            if target.as_ref() != Some(&event.location.target) {
                if state.drag_pointer() == Some(owner) {
                    state.cancel("Placement cancelled because the pointer left the scene. The pad is unchanged.");
                }
                continue;
            }
            let at = event.location.position;
            let blocked_ui = over_ui(&input.ui, camera_entity, camera, at);
            match event.action {
                PointerAction::Cancel => {
                    if state.drag_pointer() == Some(owner)
                        || matches!(state.phase, Phase::Preparing { .. } | Phase::Armed(_))
                    {
                        state.cancel("Placement cancelled because the pointer was interrupted. The pad is unchanged.");
                    }
                    pending_move = None;
                }
                PointerAction::Press(PointerButton::Primary) => {
                    if state.drag_pointer().is_some_and(|pointer| pointer != owner) {
                        state.cancel("Placement cancelled. Use one pointer to drag the pad, or use the placement buttons.");
                        continue;
                    }
                    if blocked_ui {
                        continue;
                    }
                    if matches!(state.phase, Phase::Armed(_)) && reason.is_none() {
                        let actual = geometry::actual_preview(&snapshot, state.indices);
                        let preview = state.edit().and_then(|edit| edit.preview);
                        let grabbed = [preview, actual].into_iter().flatten().find(|preview| {
                            geometry::viewport_ray(camera, global, at)
                                .is_some_and(|ray| geometry::pad_hit(ray, preview.site))
                                || geometry::grip(camera, global, preview.site)
                                    .is_some_and(|grip| grip.hit(at))
                        });
                        if let Some(initial) = grabbed {
                            if let Some(edit) = state.edit()
                                && let Ok(anchor) = camera.world_to_viewport(
                                    global,
                                    geometry::render_position(DVec3::from_array(
                                        initial.site.surface_position_m,
                                    )),
                                )
                            {
                                let source = edit.source;
                                state.start_drag(
                                    source,
                                    initial,
                                    owner,
                                    at.to_array(),
                                    anchor.to_array(),
                                );
                            }
                        } else {
                            let preview = geometry::surface_preview(camera, global, at);
                            if let Some(edit) = state.edit_mut() {
                                edit.preview = preview;
                            }
                            state.notice = preview.is_none().then(|| {
                                "No Phobos terrain at that point. Choose a visible point on Phobos."
                                    .into()
                            });
                        }
                    } else if !state.blocked()
                        && let Some(initial) = geometry::actual_preview(&snapshot, state.indices)
                    {
                        let physical_hit = geometry::viewport_ray(camera, global, at)
                            .is_some_and(|ray| geometry::pad_hit(ray, initial.site));
                        let marker_hit = input.visuals.guides
                            && geometry::grip(camera, global, initial.site)
                                .is_some_and(|grip| grip.hit(at));
                        if !physical_hit && !marker_hit {
                            continue;
                        }
                        if let Some(reason) = reason {
                            // A paused-only hint must not consume a normal camera gesture.
                            state.notice = Some(reason.into());
                        } else if let Some(source) = source(&snapshot, &state)
                            && let Ok(anchor) = camera.world_to_viewport(
                                global,
                                geometry::render_position(DVec3::from_array(
                                    initial.site.surface_position_m,
                                )),
                            )
                        {
                            // Keep the grabbed corner/lift grip under the pointer.
                            // Casting from the grip itself would jump the pad by
                            // its 32-pixel display offset as soon as dragging starts.
                            state.start_drag(
                                source,
                                initial,
                                owner,
                                at.to_array(),
                                anchor.to_array(),
                            );
                        }
                    }
                }
                PointerAction::Move { .. } => {
                    if state.drag_pointer() == Some(owner) {
                        pending_move = Some((owner, at));
                    }
                }
                PointerAction::Release(PointerButton::Primary) => {
                    if state.drag_pointer() == Some(owner) {
                        let cursor = Vec2::from_array(state.surface_cursor(at.to_array()));
                        let preview = (!blocked_ui)
                            .then(|| geometry::surface_preview(camera, global, cursor))
                            .flatten();
                        state.move_drag(owner, at.to_array(), preview);
                        if let Some(operation) = state.finish_drag(
                            owner,
                            preview.is_some() && !blocked_ui && reason.is_none(),
                        ) {
                            commit(&mut state, operation);
                        }
                    }
                    pending_move = None;
                }
                PointerAction::Press(_)
                | PointerAction::Release(_)
                | PointerAction::Scroll { .. } => {}
            }
        }
        if let Some((owner, at)) = pending_move {
            let cursor = Vec2::from_array(state.surface_cursor(at.to_array()));
            let preview = (!over_ui(&input.ui, camera_entity, camera, at))
                .then(|| geometry::surface_preview(camera, global, cursor))
                .flatten();
            state.move_drag(owner, at.to_array(), preview);
        }
        if let Some(owner) = state.drag_pointer() {
            let current = input
                .pointers
                .iter()
                .find(|(id, _, _)| pointer(**id) == Some(owner));
            if current.is_none_or(|(_, location, press)| {
                !press.is_primary_pressed() || location.location().is_none()
            }) {
                state.cancel(
                    "Placement cancelled because the pointer was lost. The pad is unchanged.",
                );
            }
        }
    }
    state.end_frame(held);
    publish(
        &state,
        reason,
        view.map(|(_, camera, global, ..)| (camera, global)),
    );
}

fn nudge(snapshot: &crate::model::Snapshot, state: &mut Placement, step: SurfaceStep) {
    if matches!(
        state.phase,
        Phase::Preparing { .. } | Phase::Dragging { .. }
    ) || (!state.active() && state.blocked())
    {
        return;
    }
    let Some(initial) = geometry::actual_preview(snapshot, state.indices) else {
        state.notice =
            Some("The actual launchpad is unavailable. Reload a valid flight archive.".into());
        return;
    };
    if !state.active() {
        let Some(source) = source(snapshot, state) else {
            return;
        };
        state.arm(source);
    }
    if let Err(error) = state.nudge(step, initial) {
        state.notice = Some(error);
    }
}

fn publish(state: &Placement, reason: Option<&str>, view: Option<(&Camera, &GlobalTransform)>) {
    let status = if let Some(notice) = &state.notice {
        notice.clone()
    } else {
        match state.phase {
            Phase::Preparing { .. } => PREPARING.into(),
            Phase::Dragging { edit, .. } if edit.preview.is_none() =>
                "No terrain under the pointer. Return to Phobos; releasing here cancels.".into(),
            Phase::Dragging { .. } =>
                "Preview only. Release on Phobos to move pad + lander. Esc cancels.".into(),
            Phase::Armed(edit) if edit.preview.is_some_and(|preview| view.is_some_and(|(camera, global)| geometry::grip(camera, global, preview.site).is_none())) =>
                "Preview is outside this view. Apply to use this site, or Cancel and start again.".into(),
            Phase::Armed(edit) if edit.preview.is_some() =>
                "Drag the pad or lift grip; release moves pad + lander. Or click terrain, then Apply or Enter. Arrows step 100 m; Esc cancels.".into(),
            Phase::Armed(_) =>
                "Click Phobos or use arrows for 100 m steps. Apply or Enter applies; Esc cancels.".into(),
            Phase::Idle => reason.unwrap_or(if crate::scene::visual_settings().guides {
                "Drag the pad or lift grip, or choose Place lander. Flight target stays unchanged."
            } else {
                "Choose Place lander to reveal its lift grip, or drag the pad while paused."
            }).into(),
        }
    };
    BRIDGE.with(|bridge| {
        let mut bridge = bridge.borrow_mut();
        bridge.blocked = state.blocked();
        bridge.status = status;
        bridge.preview_direction = state
            .edit()
            .and_then(|edit| edit.preview)
            .map(|preview| preview.direction);
    });
}

fn draw(
    mut state: ResMut<Placement>,
    rig: Res<CameraRig>,
    hud: Res<crate::hud::HudState>,
    visuals: Res<crate::scene::VisualSettings>,
    playback: Res<PlaybackState>,
    cameras: Query<(&Camera, &GlobalTransform), With<SimulationCamera>>,
    mut gizmos: Gizmos,
) {
    let snapshot = crate::snapshot();
    // Explicit native/API actions can apply after Input. Clear their stale
    // preview before rendering, without sending any compensating physics command.
    if state.active()
        && ((!state.preparing() && rig.mode.sensor().is_some())
            || playback.running
            || crate::session::is_running()
            || !playback.can_control
            || hud.launchpad_input_blocked()
            || (!state.preparing()
                && state.edit().is_some_and(|edit| {
                    !edit.source.matches(
                        &snapshot,
                        crate::session::trial_revision(),
                        crate::session::is_running(),
                    )
                })))
    {
        state.cancel(
            "Placement cancelled because the view or flight changed. The pad is unchanged.",
        );
        publish(&state, None, cameras.single().ok());
    }
    if rig.mode.sensor().is_some() || state.preparing() {
        return;
    }
    let guides = visuals.guides;
    if !guides && !state.active() {
        return;
    }
    if let Some(site) = geometry::actual_site(&snapshot, state.indices) {
        draw_box(
            &mut gizmos,
            site.pad_position_m,
            site.rotation,
            crate::scenario::settings().pad.half_extents_m,
            Color::srgb(0.96, 0.9, 0.64),
        );
        if let Ok((camera, global)) = cameras.single()
            && let Some(grip) = geometry::grip(camera, global, site)
        {
            draw_grip(&mut gizmos, grip, false);
        }
    }
    if let Some(preview) = state.edit().and_then(|edit| edit.preview) {
        // Wire shapes are previews, not relocated scene entities. Default gizmos
        // already use debug layer 1; mounted RGB cameras only render layers 0/2.
        let color = Color::srgb(0.55, 1.0, 0.88);
        draw_box(
            &mut gizmos,
            preview.site.pad_position_m,
            preview.site.rotation,
            crate::scenario::settings().pad.half_extents_m,
            color,
        );
        draw_box(
            &mut gizmos,
            preview.site.lander_position_m,
            preview.site.rotation,
            crate::scenario::settings().lander.hull_half_extents_m,
            color,
        );
        if let Ok((camera, global)) = cameras.single()
            && let Some(grip) = geometry::grip(camera, global, preview.site)
        {
            draw_grip(&mut gizmos, grip, true);
        }
    }
}

fn draw_box(
    gizmos: &mut Gizmos,
    position: [f64; 3],
    rotation: [f64; 4],
    half: [f64; 3],
    color: Color,
) {
    let position = DVec3::from_array(position);
    let rotation = DQuat::from_array(rotation);
    let half = DVec3::from_array(half);
    for axis in 0..3 {
        for a in [-1.0, 1.0] {
            for b in [-1.0, 1.0] {
                let mut from = DVec3::ZERO;
                from[axis] = -half[axis];
                from[(axis + 1) % 3] = a * half[(axis + 1) % 3];
                from[(axis + 2) % 3] = b * half[(axis + 2) % 3];
                let mut to = from;
                to[axis] = half[axis];
                gizmos.line(
                    geometry::render_position(position + rotation * from),
                    geometry::render_position(position + rotation * to),
                    color,
                );
            }
        }
    }
}

fn draw_grip(gizmos: &mut Gizmos, grip: Grip, preview: bool) {
    let color = if preview {
        Color::srgb(0.55, 1.0, 0.88)
    } else {
        Color::srgb(0.96, 0.9, 0.64)
    };
    let at = grip.handle_m;
    let right = grip.right_m;
    let up = grip.up_m;
    gizmos.line(
        geometry::render_position(grip.pad_m),
        geometry::render_position(at - up * 12.0),
        color,
    );
    // A square grip marks the 28x28 CSS-pixel hit target. The preview has a
    // second diamond, so its meaning is not carried by color alone.
    let corners = [
        at - right * 14.0 - up * 14.0,
        at + right * 14.0 - up * 14.0,
        at + right * 14.0 + up * 14.0,
        at - right * 14.0 + up * 14.0,
    ];
    for index in 0..4 {
        gizmos.line(
            geometry::render_position(corners[index]),
            geometry::render_position(corners[(index + 1) % 4]),
            color,
        );
    }
    gizmos.line(
        geometry::render_position(at - right * 7.0),
        geometry::render_position(at + right * 7.0),
        color,
    );
    if preview {
        let corners = [
            at + up * 8.0,
            at + right * 8.0,
            at - up * 8.0,
            at - right * 8.0,
        ];
        for index in 0..4 {
            gizmos.line(
                geometry::render_position(corners[index]),
                geometry::render_position(corners[(index + 1) % 4]),
                color,
            );
        }
    }
}
