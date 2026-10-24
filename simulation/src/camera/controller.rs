use super::model::{CameraError, CameraRig, CameraStatus};
use super::movement::DebugFlight;
use super::overview;
use super::zoom::{self, MAX_DISTANCE_KM, MIN_DISTANCE_KM};
use crate::{
    domain::{CameraMode, CameraRequest, EngineAction, EntityId, EntityKind, ViewportAction},
    model::Snapshot,
};
use bevy::{
    camera::visibility::RenderLayers,
    ecs::system::SystemParam,
    input::{
        gestures::PinchGesture,
        mouse::{AccumulatedMouseMotion, AccumulatedMouseScroll, MouseScrollUnit},
    },
    input_focus::InputFocus,
    picking::hover::Hovered,
    prelude::*,
    ui::Pressed,
    ui_widgets::{Button as UiButton, ScrollArea},
};
use std::{
    cell::{Cell, RefCell},
    collections::VecDeque,
};

const KM: f32 = 0.001;

#[derive(Component)]
pub(crate) struct SimulationCamera;

#[derive(Clone, Copy, Default)]
struct InputGate {
    active: bool,
    locked: bool,
}

#[derive(Default)]
struct PointerGesture {
    start: Option<Vec2>,
    dragged: bool,
    blocked: bool,
}

thread_local! {
    static COMMANDS: RefCell<VecDeque<CameraRequest>> = const { RefCell::new(VecDeque::new()) };
    static GATE: RefCell<InputGate> = const { RefCell::new(InputGate { active: false, locked: false }) };
    static STATUS: RefCell<CameraStatus> = RefCell::new(CameraStatus::default());
    static PINCH_ZOOM: Cell<f32> = const { Cell::new(0.0) };
}

pub(super) fn install(app: &mut App) {
    app.init_resource::<CameraRig>()
        .add_systems(Update, update_camera.in_set(crate::EngineSet::Camera));
}

pub(super) fn set_input_active(active: bool, locked: bool) {
    GATE.with(|gate| *gate.borrow_mut() = InputGate { active, locked });
    if !active {
        PINCH_ZOOM.with(|pending| pending.set(0.0));
    }
}

pub(super) fn input_active() -> bool {
    GATE.with(|gate| gate.borrow().active)
}

pub(super) fn pinch_zoom(log_distance_delta: f32) {
    if input_active() && log_distance_delta.is_finite() {
        PINCH_ZOOM
            .with(|pending| pending.set((pending.get() + log_distance_delta).clamp(-1.5, 1.5)));
    }
}

pub(super) fn status() -> CameraStatus {
    STATUS.with(|status| *status.borrow())
}

pub(super) fn request_view(request: CameraRequest) -> Result<(), CameraError> {
    if request
        .zoom
        .is_some_and(|zoom| !zoom.is_finite() || !(0.05..=20.0).contains(&zoom))
    {
        return Err(CameraError::InvalidZoom);
    }
    if let Some(focus) = request.focus
        && !focus_exists(focus, &crate::snapshot())
    {
        return Err(CameraError::UnknownFocus(focus));
    }
    COMMANDS.with(|queue| queue.borrow_mut().push_back(request));
    Ok(())
}

type UiHover = (Entity, &'static Hovered, Has<Pressed>);
type CameraUi = Or<(With<UiButton>, With<ScrollArea>)>;

#[derive(SystemParam)]
struct CameraInput<'w, 's> {
    time: Res<'w, Time>,
    keys: Res<'w, ButtonInput<KeyCode>>,
    buttons: Res<'w, ButtonInput<MouseButton>>,
    input_focus: Res<'w, InputFocus>,
    hud: Res<'w, crate::hud::HudState>,
    motion: Res<'w, AccumulatedMouseMotion>,
    scroll: Res<'w, AccumulatedMouseScroll>,
    pinches: MessageReader<'w, 's, PinchGesture>,
    windows: Query<'w, 's, &'static Window>,
    ui: Query<'w, 's, UiHover, CameraUi>,
}

fn update_camera(
    input: CameraInput,
    mut cameras: Query<
        (
            &Camera,
            &GlobalTransform,
            &mut Transform,
            &mut RenderLayers,
            &mut Projection,
        ),
        With<SimulationCamera>,
    >,
    mut rig: ResMut<CameraRig>,
    mut gesture: Local<PointerGesture>,
    mut flight: Local<DebugFlight>,
) {
    let CameraInput {
        time,
        keys,
        buttons,
        input_focus,
        hud,
        motion,
        scroll,
        mut pinches,
        windows,
        ui,
    } = input;
    let pinch_delta = PINCH_ZOOM.with(|pending| pending.replace(0.0))
        - pinches
            .read()
            .map(|gesture| gesture.0)
            .filter(|delta| delta.is_finite())
            .sum::<f32>();
    let gate = GATE.with(|gate| *gate.borrow());
    if !gate.active {
        gesture.start = None;
        gesture.dragged = false;
        gesture.blocked = false;
    }

    let Ok((camera, global, mut transform, mut layers, mut projection)) = cameras.single_mut()
    else {
        return;
    };
    let snapshot = crate::snapshot();
    let previous_mode = rig.mode;
    if !rig.initialized {
        flight.preserve_pose(&mut rig, transform.rotation);
        rig.distance_km = base_distance_km(rig.focus, &snapshot, rig.mode) * rig.zoom;
        rig.initialized = true;
    }
    let mut mode_changed = false;
    while let Some(request) = COMMANDS.with(|queue| queue.borrow_mut().pop_front()) {
        mode_changed |= request.mode.is_some_and(|mode| rig.mode != mode);
        apply_view_request(&mut rig, request, &snapshot);
    }
    if mode_changed && rig.mode == CameraMode::FreeFlight {
        flight.preserve_pose(&mut rig, transform.rotation);
    }
    if !focus_exists(rig.focus, &snapshot) {
        rig.focus = EntityId::Lander;
        rig.pan_offset = Vec3::ZERO;
        rig.distance_km = (base_distance_km(rig.focus, &snapshot, rig.mode) * rig.zoom)
            .clamp(MIN_DISTANCE_KM, MAX_DISTANCE_KM);
    }
    let mut target = focus_target(&rig, &snapshot);

    let over_ui = ui
        .iter()
        .any(|(_, hovered, pressed)| hovered.get() || pressed);
    let button_focused = input_focus.get().is_some_and(|entity| ui.contains(entity));
    let cursor = windows.single().ok().and_then(Window::cursor_position);
    let dt = time.delta_secs().min(0.1);
    let binding_actions_blocked = crate::controls::binding_actions_blocked();
    let keyboard_blocked = hud.movement_blocked() || binding_actions_blocked;
    if gate.active && !button_focused && !keyboard_blocked {
        if keys.just_pressed(KeyCode::Escape) {
            crate::request_action(EngineAction::Viewport(ViewportAction::Release));
            set_input_active(false, false);
        }
        if keys.just_pressed(KeyCode::Digit1) {
            rig.mode = CameraMode::Orbit;
            rig.distance_km = base_distance_km(rig.focus, &snapshot, rig.mode) * rig.zoom;
        }
        if keys.just_pressed(KeyCode::Digit2) {
            rig.mode = CameraMode::FirstPerson;
            rig.yaw = 0.0;
            rig.pitch = 0.0;
        }
        if keys.just_pressed(KeyCode::Digit3) {
            rig.mode = CameraMode::ThirdPerson;
            rig.distance_km = base_distance_km(rig.focus, &snapshot, rig.mode) * rig.zoom;
        }
        if keys.just_pressed(KeyCode::Digit4) {
            flight.preserve_pose(&mut rig, transform.rotation);
            rig.mode = CameraMode::FreeFlight;
        }
        if keys.just_pressed(KeyCode::Digit5) {
            overview::frame(&mut rig, &snapshot);
        }
        if keys.just_pressed(KeyCode::Digit6) {
            rig.mode = CameraMode::ShipTop;
        }
        if keys.just_pressed(KeyCode::Digit7) {
            rig.mode = CameraMode::ShipBottom;
        }
        if rig.mode.sensor().is_none() && keys.pressed(KeyCode::ArrowLeft) {
            rig.yaw += dt;
        }
        if rig.mode.sensor().is_none() && keys.pressed(KeyCode::ArrowRight) {
            rig.yaw -= dt;
        }
        if rig.mode.sensor().is_none() && keys.pressed(KeyCode::ArrowUp) {
            rig.pitch = (rig.pitch + dt).clamp(-1.52, 1.52);
        }
        if rig.mode.sensor().is_none() && keys.pressed(KeyCode::ArrowDown) {
            rig.pitch = (rig.pitch - dt).clamp(-1.52, 1.52);
        }
    }

    if gate.active && !hud.movement_blocked() {
        if buttons.just_pressed(MouseButton::Left)
            || buttons.just_pressed(MouseButton::Right)
            || buttons.just_pressed(MouseButton::Middle)
        {
            gesture.blocked = over_ui;
        }
        if let Some(cursor) = cursor {
            if buttons.just_pressed(MouseButton::Left) {
                gesture.start = Some(cursor);
                gesture.dragged = false;
                gesture.blocked = over_ui;
            }
            if let Some(start) = gesture.start {
                if cursor.distance(start) > 4.0 {
                    gesture.dragged = true;
                }
                if buttons.just_released(MouseButton::Left) {
                    if !gesture.dragged
                        && !gesture.blocked
                        && !over_ui
                        && rig.mode.sensor().is_none()
                    {
                        pick_focus(camera, global, cursor, &snapshot, &mut rig);
                    }
                    gesture.start = None;
                }
            }
        } else if buttons.just_released(MouseButton::Left) {
            gesture.start = None;
        }

        let left_drag = buttons.pressed(MouseButton::Left);
        let right_drag = buttons.pressed(MouseButton::Right);
        let middle_drag = buttons.pressed(MouseButton::Middle);
        let shift = keys.pressed(KeyCode::ShiftLeft) || keys.pressed(KeyCode::ShiftRight);
        if !over_ui
            && !gesture.blocked
            && motion.delta != Vec2::ZERO
            && (gate.locked || left_drag || right_drag || middle_drag)
        {
            match rig.mode {
                CameraMode::FreeFlight if gate.locked || right_drag || left_drag => {
                    rotate_camera(&mut rig, motion.delta)
                }
                CameraMode::FirstPerson => rotate_camera(&mut rig, motion.delta),
                CameraMode::Orbit | CameraMode::ThirdPerson | CameraMode::Overview
                    if right_drag || middle_drag || (left_drag && shift) =>
                {
                    let delta = (-transform.rotation * Vec3::X * motion.delta.x
                        + transform.rotation * Vec3::Y * motion.delta.y)
                        * rig.distance_km
                        * 0.0015;
                    rig.pan_offset += delta;
                    target += delta;
                }
                CameraMode::Orbit | CameraMode::ThirdPerson | CameraMode::Overview if left_drag => {
                    rotate_camera(&mut rig, motion.delta)
                }
                _ => {}
            }
        }

        if !over_ui && scroll.delta.y != 0.0 && rig.mode.sensor().is_none() {
            let lines = match scroll.unit {
                MouseScrollUnit::Line => scroll.delta.y,
                MouseScrollUnit::Pixel => scroll.delta.y / 40.0,
            };
            let factor = (-lines.clamp(-8.0, 8.0) * 0.12).exp();
            if rig.mode == CameraMode::FreeFlight {
                rig.speed_mps = (rig.speed_mps / factor).clamp(1.0, 100_000.0);
            } else {
                rig.distance_km =
                    (rig.distance_km * factor).clamp(MIN_DISTANCE_KM, MAX_DISTANCE_KM);
                rig.zoom = (rig.distance_km / base_distance_km(rig.focus, &snapshot, rig.mode))
                    .clamp(0.0001, 10_000.0);
            }
        }

        if !over_ui && pinch_delta != 0.0 {
            let base_distance = base_distance_km(rig.focus, &snapshot, rig.mode);
            zoom::apply_pinch(
                &mut rig,
                &mut transform,
                &mut projection,
                target,
                base_distance,
                pinch_delta,
            );
        }

        // Dedicated movement ignores stale button focus, but not the reset modal.
        flight.translate(
            &mut rig,
            &mut transform,
            &crate::controls::movement(&keys),
            input_active(),
            hud.movement_blocked(),
            dt,
        );
    }

    // Dedicated follow ignores stale button focus, but not browser chords,
    // a modal or the rest of a consumed binding-input frame.
    if let Some(request) = frame_shortcut(&keys, input_active(), keyboard_blocked, rig.focus) {
        apply_view_request(&mut rig, request, &snapshot);
    }

    target = focus_target(&rig, &snapshot);
    if previous_mode == CameraMode::Overview
        && rig.mode != CameraMode::Overview
        && let Projection::Perspective(lens) = &mut *projection
    {
        lens.fov = 60.0_f32.to_radians();
    }
    *layers = if rig.mode.sensor().is_some() {
        RenderLayers::none()
    } else {
        RenderLayers::layer(0).with(1)
    };
    match rig.mode {
        CameraMode::FreeFlight => transform.rotation = flight.rotation(&rig),
        CameraMode::FirstPerson => {
            let body_rotation = focus_entity(rig.focus, &snapshot)
                .map(|entity| rotation(entity.rotation))
                .unwrap_or(Quat::IDENTITY);
            transform.translation =
                outside_body(target + body_rotation * Vec3::new(0.0, 0.0, 0.006));
            transform.translation = outside_mars(transform.translation, &snapshot);
            transform.rotation = body_rotation
                * Quat::from_rotation_y(std::f32::consts::PI + rig.yaw)
                * Quat::from_rotation_x(-rig.pitch);
        }
        CameraMode::ThirdPerson => {
            let body_rotation = focus_entity(rig.focus, &snapshot)
                .map(|entity| rotation(entity.rotation))
                .unwrap_or(Quat::IDENTITY);
            let mut position =
                target + body_rotation * orbit_offset(&rig, rig.distance_km.max(0.15));
            position = outside_body(position);
            position = outside_mars(position, &snapshot);
            transform.translation = position;
            transform.look_at(target, Vec3::Y);
        }
        CameraMode::Overview => {
            let view = overview::view(&rig, &snapshot, &mut projection);
            transform.translation = view.translation;
            transform.rotation = view.rotation;
        }
        CameraMode::Orbit => {
            let view = orbit_view(&rig, &snapshot);
            transform.translation = view.translation;
            transform.rotation = view.rotation;
        }
        CameraMode::ShipTop | CameraMode::ShipBottom => {}
    }
    STATUS.with(|status| {
        let sensor_pose = rig
            .mode
            .sensor()
            .map(|id| crate::sensors::pose(id, &snapshot));
        let view = sensor_pose.as_ref().unwrap_or(&transform);
        let next = CameraStatus::from_view(
            &rig,
            view,
            focus_position(
                if sensor_pose.is_some() {
                    EntityId::Lander
                } else {
                    rig.focus
                },
                &snapshot,
            ),
            gate.active,
            gate.locked,
        );
        *status.borrow_mut() = next;
    });
}

fn frame_shortcut(
    keys: &ButtonInput<KeyCode>,
    active: bool,
    blocked: bool,
    selected: EntityId,
) -> Option<CameraRequest> {
    if !active
        || blocked
        || !keys.just_pressed(KeyCode::KeyF)
        || keys.any_pressed([
            KeyCode::ControlLeft,
            KeyCode::ControlRight,
            KeyCode::SuperLeft,
            KeyCode::SuperRight,
            KeyCode::AltLeft,
            KeyCode::AltRight,
        ])
    {
        return None;
    }
    Some(CameraRequest {
        mode: Some(CameraMode::Orbit),
        focus: Some(
            if keys.any_pressed([KeyCode::ShiftLeft, KeyCode::ShiftRight]) {
                selected
            } else {
                EntityId::Lander
            },
        ),
        zoom: Some(1.0),
    })
}

fn apply_view_request(rig: &mut CameraRig, request: CameraRequest, snapshot: &Snapshot) {
    if request.mode == Some(CameraMode::Overview) {
        overview::frame(rig, snapshot);
        if let Some(zoom) = request.zoom {
            rig.zoom = zoom;
            rig.distance_km = (rig.distance_km * zoom).clamp(MIN_DISTANCE_KM, MAX_DISTANCE_KM);
        }
        return;
    }
    if let Some(focus) = request.focus
        && request.mode == Some(CameraMode::Orbit)
        && request.zoom == Some(1.0)
    {
        frame_focus(rig, focus, snapshot);
        return;
    }
    let mode_changed = request.mode.is_some_and(|mode| rig.mode != mode);
    if let Some(mode) = request.mode {
        rig.mode = mode;
        if mode_changed && mode == CameraMode::FirstPerson {
            rig.yaw = 0.0;
            rig.pitch = 0.0;
        }
    }
    if let Some(focus) = request.focus {
        rig.focus = focus;
        rig.pan_offset = Vec3::ZERO;
    }
    if let Some(zoom) = request.zoom {
        rig.zoom = zoom;
    }
    if mode_changed || request.focus.is_some() || request.zoom.is_some() {
        rig.distance_km = (base_distance_km(rig.focus, snapshot, rig.mode) * rig.zoom)
            .clamp(MIN_DISTANCE_KM, MAX_DISTANCE_KM);
    }
}

fn frame_focus(rig: &mut CameraRig, focus: EntityId, snapshot: &Snapshot) {
    rig.mode = CameraMode::Orbit;
    rig.focus = focus;
    rig.pan_offset = Vec3::ZERO;
    rig.zoom = 1.0;
    rig.distance_km = base_distance_km(focus, snapshot, rig.mode);
    if focus != EntityId::Lander {
        return;
    }

    let target = focus_target(rig, snapshot);
    let from_mars = target - meters(snapshot.environment.mars_position_m);
    let near_mars = from_mars.length()
        <= snapshot.environment.mars_radius_m as f32 * KM + 0.25 + rig.distance_km;
    let near_body = target.length() <= body_radius_bound_km() + rig.distance_km;
    let outward = if near_mars {
        from_mars.normalize_or(Vec3::Z)
    } else {
        target.normalize_or(Vec3::NEG_X)
    };
    let offset = orbit_offset(rig, rig.distance_km);
    let position = target + offset;
    let bounded = outside_mars(outside_body(position), snapshot);
    if rig.yaw.is_finite()
        && rig.pitch.is_finite()
        && (-1.52..=1.52).contains(&rig.pitch)
        && (!(near_body || near_mars) || offset.dot(outward) >= 0.0)
        && position.distance_squared(bounded) < 1e-10
    {
        return;
    }
    // Keep usable world-space angles. Otherwise look inward from the body's outer side.
    rig.yaw = outward.x.atan2(outward.z);
    rig.pitch = outward.y.clamp(-1.0, 1.0).asin().clamp(-1.3, 1.3);
}

fn rotate_camera(rig: &mut CameraRig, delta: Vec2) {
    rig.yaw -= delta.x * 0.004;
    rig.pitch = (rig.pitch + delta.y * 0.004).clamp(-1.52, 1.52);
}

fn focus_exists(focus: EntityId, snapshot: &Snapshot) -> bool {
    match focus {
        EntityId::Phobos | EntityId::Mars | EntityId::L1 | EntityId::L2 => true,
        id => snapshot.assets.iter().any(|asset| asset.id == id),
    }
}

fn focus_entity(focus: EntityId, snapshot: &Snapshot) -> Option<&crate::model::EntityState> {
    snapshot.assets.iter().find(|asset| asset.id == focus)
}

fn focus_position(focus: EntityId, snapshot: &Snapshot) -> Vec3 {
    match focus {
        EntityId::Phobos => Vec3::ZERO,
        EntityId::Mars => meters(snapshot.environment.mars_position_m),
        EntityId::L1 => meters(snapshot.environment.l1_position_m),
        EntityId::L2 => meters(snapshot.environment.l2_position_m),
        id => focus_entity(id, snapshot)
            .map(|asset| meters(asset.position))
            .unwrap_or(Vec3::ZERO),
    }
}

fn focus_target(rig: &CameraRig, snapshot: &Snapshot) -> Vec3 {
    focus_position(rig.focus, snapshot) + rig.pan_offset
}

fn base_distance_km(focus: EntityId, snapshot: &Snapshot, mode: CameraMode) -> f32 {
    if mode == CameraMode::Overview {
        return overview::base_distance_km(snapshot);
    }
    match focus {
        EntityId::Phobos => {
            crate::model::phobos_radii_m()
                .into_iter()
                .fold(0.0_f64, f64::max) as f32
                * KM
                * 2.2
        }
        EntityId::Mars => snapshot.environment.mars_radius_m as f32 * KM * 2.2,
        EntityId::L1 | EntityId::L2 => 1.0,
        EntityId::Lander => 0.12,
        EntityId::ReturnShip => 0.25,
        EntityId::LandingPad => 0.10,
        EntityId::Debris(_) => focus_entity(focus, snapshot)
            .map_or(0.15, |entity| (entity.radius_m as f32 * KM * 4.0).max(0.05)),
    }
}

fn meters(position: [f64; 3]) -> Vec3 {
    Vec3::new(
        (position[0] * KM as f64) as f32,
        (position[1] * KM as f64) as f32,
        (position[2] * KM as f64) as f32,
    )
}

fn rotation(value: [f64; 4]) -> Quat {
    Quat::from_xyzw(
        value[0] as f32,
        value[1] as f32,
        value[2] as f32,
        value[3] as f32,
    )
    .normalize()
}

fn orbit_offset(rig: &CameraRig, distance: f32) -> Vec3 {
    let cosine_pitch = rig.pitch.cos();
    Vec3::new(
        rig.yaw.sin() * cosine_pitch,
        rig.pitch.sin(),
        rig.yaw.cos() * cosine_pitch,
    ) * distance
}

fn orbit_position(target: Vec3, rig: &CameraRig) -> Vec3 {
    target + orbit_offset(rig, rig.distance_km)
}

fn orbit_view(rig: &CameraRig, snapshot: &Snapshot) -> Transform {
    let target = focus_target(rig, snapshot);
    let position = outside_mars(outside_body(orbit_position(target, rig)), snapshot);
    // Orbit follows translation, never the lander's rolling body frame.
    Transform::from_translation(position).looking_at(target, Vec3::Y)
}

fn body_radius_bound_km() -> f32 {
    crate::model::phobos_radii_m()
        .into_iter()
        .fold(0.0_f64, f64::max) as f32
        * KM
        * 1.1
}

fn outside_body(position: Vec3) -> Vec3 {
    let radius_bound = body_radius_bound_km();
    if position.length() > radius_bound {
        return position;
    }
    let direction = position.normalize_or(Vec3::NEG_X);
    let radius_km = crate::terrain::surface_radius_m(direction.as_dvec3()) as f32 * KM;
    direction * position.length().max(radius_km + 0.003)
}

fn outside_mars(position: Vec3, snapshot: &Snapshot) -> Vec3 {
    let center = meters(snapshot.environment.mars_position_m);
    let radius = snapshot.environment.mars_radius_m as f32 * KM;
    let offset = position - center;
    if offset.length() < radius + 0.25 {
        center + offset.normalize_or(Vec3::Z) * (radius + 0.25)
    } else {
        position
    }
}

fn pick_focus(
    camera: &Camera,
    global: &GlobalTransform,
    cursor: Vec2,
    snapshot: &Snapshot,
    rig: &mut CameraRig,
) {
    let Ok(ray) = camera.viewport_to_world(global, cursor) else {
        return;
    };
    let origin = ray.origin;
    let direction = *ray.direction;
    let mut closest = f32::INFINITY;
    let mut selected = None;
    for asset in &snapshot.assets {
        let center = meters(asset.position);
        let minimum_radius = origin.distance(center) * 0.006;
        let hit = match asset.kind {
            EntityKind::LandingPad => ray_box(
                origin,
                direction,
                center,
                rotation(asset.rotation),
                crate::scenario::settings()
                    .pad
                    .half_extents_m
                    .map(|v| v as f32 * KM),
            ),
            EntityKind::Lander | EntityKind::ReturnShip | EntityKind::Debris => ray_sphere(
                origin,
                direction,
                center,
                (asset.radius_m as f32 * KM).max(minimum_radius),
            ),
        };
        if let Some(distance) = hit
            && distance < closest
        {
            closest = distance;
            selected = Some(asset.id);
        }
    }
    if let Some(distance) = ray_ellipsoid(
        origin,
        direction,
        crate::model::phobos_radii_m().map(|radius| (radius * KM as f64) as f32),
    ) && distance < closest
    {
        closest = distance;
        selected = Some(EntityId::Phobos);
    }
    if let Some(distance) = ray_sphere(
        origin,
        direction,
        meters(snapshot.environment.mars_position_m),
        snapshot.environment.mars_radius_m as f32 * KM,
    ) && distance < closest
    {
        closest = distance;
        selected = Some(EntityId::Mars);
    }
    for (id, position) in [
        (EntityId::L1, snapshot.environment.l1_position_m),
        (EntityId::L2, snapshot.environment.l2_position_m),
    ] {
        if let Some(distance) = ray_sphere(origin, direction, meters(position), 0.05)
            && distance < closest
        {
            closest = distance;
            selected = Some(id);
        }
    }
    if let Some(selected) = selected {
        rig.focus = selected;
        rig.pan_offset = Vec3::ZERO;
        rig.distance_km = (base_distance_km(selected, snapshot, rig.mode) * rig.zoom)
            .clamp(MIN_DISTANCE_KM, MAX_DISTANCE_KM);
    }
}

fn ray_sphere(origin: Vec3, direction: Vec3, center: Vec3, radius: f32) -> Option<f32> {
    let offset = origin - center;
    let projection = offset.dot(direction);
    let discriminant = projection * projection - (offset.length_squared() - radius * radius);
    if discriminant < 0.0 {
        return None;
    }
    let root = discriminant.sqrt();
    let near = -projection - root;
    let far = -projection + root;
    if near >= 0.0 {
        Some(near)
    } else if far >= 0.0 {
        Some(far)
    } else {
        None
    }
}

fn ray_box(
    origin: Vec3,
    direction: Vec3,
    center: Vec3,
    attitude: Quat,
    half_extents: [f32; 3],
) -> Option<f32> {
    let local_origin = attitude.inverse() * (origin - center);
    let local_direction = attitude.inverse() * direction;
    let (mut near, mut far) = (0.0_f32, f32::INFINITY);
    for axis in 0..3 {
        let (p, d, half) = (
            local_origin[axis],
            local_direction[axis],
            half_extents[axis],
        );
        if d.abs() < 1e-8 {
            if p.abs() > half {
                return None;
            }
        } else {
            let a = (-half - p) / d;
            let b = (half - p) / d;
            near = near.max(a.min(b));
            far = far.min(a.max(b));
            if near > far {
                return None;
            }
        }
    }
    Some(near)
}

fn ray_ellipsoid(origin: Vec3, direction: Vec3, radii: [f32; 3]) -> Option<f32> {
    let scale = Vec3::new(1.0 / radii[0], 1.0 / radii[1], 1.0 / radii[2]);
    let scaled_origin = origin * scale;
    let scaled_direction = direction * scale;
    let a = scaled_direction.length_squared();
    let b = 2.0 * scaled_origin.dot(scaled_direction);
    let c = scaled_origin.length_squared() - 1.0;
    let discriminant = b * b - 4.0 * a * c;
    if discriminant < 0.0 {
        return None;
    }
    let root = discriminant.sqrt();
    let near = (-b - root) / (2.0 * a);
    let far = (-b + root) / (2.0 * a);
    if near >= 0.0 {
        Some(near)
    } else if far >= 0.0 {
        Some(far)
    } else {
        None
    }
}

#[cfg(all(test, not(target_arch = "wasm32")))]
mod tests {
    use super::*;
    use crate::{domain::ShipCameraId, physics::Simulation};

    fn keys(pressed: &[KeyCode]) -> ButtonInput<KeyCode> {
        let mut keys = ButtonInput::default();
        for key in pressed {
            keys.press(*key);
        }
        keys
    }

    fn close(actual: Vec3, expected: Vec3) {
        assert!(
            actual.distance(expected) < 2e-6,
            "{actual:?} != {expected:?}"
        );
    }

    fn camera_app(mode: CameraMode) -> App {
        crate::controls::reset_for_tests();
        COMMANDS.with(|commands| commands.borrow_mut().clear());
        PINCH_ZOOM.with(|pending| pending.set(0.0));
        STATUS.with(|status| *status.borrow_mut() = CameraStatus::default());
        set_input_active(true, false);

        let mut app = App::new();
        app.init_resource::<Time>()
            .init_resource::<ButtonInput<KeyCode>>()
            .init_resource::<ButtonInput<MouseButton>>()
            .init_resource::<InputFocus>()
            .init_resource::<crate::hud::HudState>()
            .init_resource::<AccumulatedMouseMotion>()
            .init_resource::<AccumulatedMouseScroll>()
            .add_message::<PinchGesture>()
            .insert_resource(CameraRig {
                mode,
                focus: EntityId::Mars,
                yaw: 0.3,
                pitch: -0.2,
                distance_km: 42.0,
                zoom: 2.0,
                pan_offset: Vec3::ONE,
                initialized: true,
                ..default()
            });
        install(&mut app);
        app.world_mut().spawn((
            SimulationCamera,
            Camera::default(),
            GlobalTransform::default(),
            Transform::default(),
            RenderLayers::layer(0).with(1),
            Projection::Perspective(PerspectiveProjection::default()),
        ));
        app
    }

    fn capture_cancel_and_release(app: &mut App, key: KeyCode) {
        crate::controls::begin_capture(crate::controls::Action::Front);
        crate::controls::begin_input_frame();
        let mut keys = app.world_mut().resource_mut::<ButtonInput<KeyCode>>();
        if key != KeyCode::Escape {
            keys.press(key);
            crate::controls::capture_key(key, false);
            assert!(crate::controls::binding_capture_active());
            keys.release(key);
        }
        keys.press(KeyCode::Escape);
        crate::controls::capture_key(KeyCode::Escape, false);
        keys.release(KeyCode::Escape);
        crate::controls::observe_input_release(&keys);
        assert!(!crate::controls::manual_input_blocked());
        assert!(crate::controls::binding_actions_blocked());
        assert!(keys.just_pressed(key));
        assert!(keys.just_released(key));
    }

    #[test]
    fn scheduled_camera_consumes_shortcuts_after_same_frame_capture_cancel_and_release() {
        for (key, next_mode) in [
            (KeyCode::KeyF, CameraMode::Orbit),
            (KeyCode::Digit1, CameraMode::Orbit),
            (KeyCode::Digit2, CameraMode::FirstPerson),
            (KeyCode::Digit3, CameraMode::ThirdPerson),
            (KeyCode::Digit4, CameraMode::FreeFlight),
            (KeyCode::Digit5, CameraMode::Overview),
            (KeyCode::Digit6, CameraMode::ShipTop),
            (KeyCode::Digit7, CameraMode::ShipBottom),
            (KeyCode::Escape, CameraMode::FreeFlight),
        ] {
            let initial_mode = if key == KeyCode::Digit4 {
                CameraMode::Orbit
            } else {
                CameraMode::FreeFlight
            };
            let mut app = camera_app(initial_mode);
            let queued = crate::session::queued_actions_for_tests();
            capture_cancel_and_release(&mut app, key);
            assert!(
                !app.world()
                    .resource::<crate::hud::HudState>()
                    .movement_blocked()
            );

            app.update();
            let rig = app.world().resource::<CameraRig>();
            assert_eq!(
                rig.mode, initial_mode,
                "captured {key:?} changed the camera mode"
            );
            assert_eq!(rig.focus, EntityId::Mars);
            assert_eq!(rig.pan_offset, Vec3::ONE);
            assert_eq!(rig.zoom, 2.0);
            assert_eq!(rig.distance_km, 42.0);
            assert_eq!((rig.yaw, rig.pitch), (0.3, -0.2));
            assert!(
                input_active(),
                "captured Escape must not release camera input"
            );
            assert_eq!(crate::session::queued_actions_for_tests(), queued);

            app.world_mut()
                .resource_mut::<ButtonInput<KeyCode>>()
                .clear();
            crate::controls::begin_input_frame();
            assert!(!crate::controls::binding_actions_blocked());
            if key == KeyCode::Escape {
                continue;
            }
            app.world_mut()
                .resource_mut::<ButtonInput<KeyCode>>()
                .press(key);
            app.update();
            let rig = app.world().resource::<CameraRig>();
            assert_eq!(
                rig.mode, next_mode,
                "a fresh {key:?} must work after the consumed frame"
            );
            if key == KeyCode::KeyF {
                assert_eq!(rig.focus, EntityId::Lander);
                assert_eq!(rig.pan_offset, Vec3::ZERO);
                assert_eq!(rig.zoom, 1.0);
                assert_eq!(rig.distance_km, 0.12);
            }
        }
        crate::controls::reset_for_tests();
    }

    #[test]
    fn consumed_keyboard_frame_keeps_mouse_navigation_and_explicit_view_requests() {
        let mut app = camera_app(CameraMode::Orbit);
        let before = crate::snapshot();
        let sensors =
            [ShipCameraId::Top, ShipCameraId::Bottom].map(|id| crate::sensors::pose(id, &before));
        let queued = crate::session::queued_actions_for_tests();
        capture_cancel_and_release(&mut app, KeyCode::Escape);
        request_view(CameraRequest {
            mode: Some(CameraMode::FirstPerson),
            focus: Some(EntityId::ReturnShip),
            zoom: Some(1.5),
        })
        .unwrap();
        app.world_mut()
            .resource_mut::<ButtonInput<MouseButton>>()
            .press(MouseButton::Right);
        app.world_mut()
            .resource_mut::<AccumulatedMouseMotion>()
            .delta = Vec2::new(25.0, -10.0);

        app.update();
        let rig = app.world().resource::<CameraRig>();
        assert_eq!(rig.mode, CameraMode::FirstPerson);
        assert_eq!(rig.focus, EntityId::ReturnShip);
        assert_eq!(rig.zoom, 1.5);
        assert!((rig.yaw + 0.1).abs() < 1e-6);
        assert!((rig.pitch + 0.04).abs() < 1e-6);
        assert!(input_active());
        assert_eq!(crate::session::queued_actions_for_tests(), queued);
        let after = crate::snapshot();
        assert_eq!(
            serde_json::to_value(after.as_ref()).unwrap(),
            serde_json::to_value(before.as_ref()).unwrap()
        );
        assert_eq!(
            [ShipCameraId::Top, ShipCameraId::Bottom].map(|id| crate::sensors::pose(id, &after)),
            sensors
        );
        crate::controls::reset_for_tests();
    }

    #[test]
    fn f_always_relocks_the_lander_from_every_mode_and_focus() {
        let snapshot = Simulation::new_default().snapshot();
        let keys = keys(&[KeyCode::KeyF]);
        for mode in [
            CameraMode::Orbit,
            CameraMode::FirstPerson,
            CameraMode::ThirdPerson,
            CameraMode::FreeFlight,
            CameraMode::Overview,
            CameraMode::ShipTop,
            CameraMode::ShipBottom,
        ] {
            for focus in [
                EntityId::Phobos,
                EntityId::Mars,
                EntityId::L1,
                EntityId::L2,
                EntityId::Lander,
                EntityId::ReturnShip,
                EntityId::LandingPad,
                EntityId::Debris(1),
            ] {
                let mut rig = CameraRig {
                    mode,
                    focus,
                    pan_offset: Vec3::new(5.0, -3.0, 1.0),
                    zoom: 7.0,
                    distance_km: 42.0,
                    speed_mps: 321.0,
                    ..default()
                };
                let request = frame_shortcut(&keys, true, false, rig.focus).unwrap();
                assert_eq!(request.mode, Some(CameraMode::Orbit));
                assert_eq!(request.focus, Some(EntityId::Lander));
                assert_eq!(request.zoom, Some(1.0));
                apply_view_request(&mut rig, request, &snapshot);
                assert_eq!(rig.mode, CameraMode::Orbit);
                assert_eq!(rig.focus, EntityId::Lander);
                assert_eq!(rig.pan_offset, Vec3::ZERO);
                assert_eq!(rig.zoom, 1.0);
                assert_eq!(rig.distance_km, 0.12);
                assert_eq!(rig.speed_mps, 321.0);
                let view = orbit_view(&rig, &snapshot);
                let target = focus_target(&rig, &snapshot);
                close(
                    view.rotation * Vec3::NEG_Z,
                    (target - view.translation).normalize(),
                );
                let status = CameraStatus::from_view(&rig, &view, target, true, false);
                assert_eq!(status.mode, CameraMode::Orbit);
                assert_eq!(status.focus, EntityId::Lander);
            }
        }
    }

    #[test]
    fn hud_follow_request_relocks_again_without_touching_physics_or_sensors() {
        let simulation = Simulation::new_default();
        let snapshot = simulation.snapshot();
        let before = serde_json::to_value(&snapshot).unwrap();
        let sensor_poses =
            [ShipCameraId::Top, ShipCameraId::Bottom].map(|id| crate::sensors::pose(id, &snapshot));
        let mut rig = CameraRig {
            mode: CameraMode::ShipBottom,
            focus: EntityId::Mars,
            pan_offset: Vec3::ONE,
            zoom: 0.05,
            ..default()
        };
        let request = CameraRequest {
            mode: Some(CameraMode::Orbit),
            focus: Some(EntityId::Lander),
            zoom: Some(1.0),
        };
        apply_view_request(&mut rig, request, &snapshot);
        let angles = (rig.yaw, rig.pitch);
        rig.pan_offset = Vec3::new(1.0, 2.0, 3.0);
        rig.distance_km = 0.8;
        rig.zoom = 4.0;
        apply_view_request(&mut rig, request, &snapshot);
        assert_eq!(rig.mode, CameraMode::Orbit);
        assert_eq!(rig.focus, EntityId::Lander);
        assert_eq!(rig.pan_offset, Vec3::ZERO);
        assert_eq!(rig.zoom, 1.0);
        assert_eq!(rig.distance_km, 0.12);
        assert_eq!((rig.yaw, rig.pitch), angles);
        assert_eq!(
            [ShipCameraId::Top, ShipCameraId::Bottom].map(|id| crate::sensors::pose(id, &snapshot)),
            sensor_poses
        );
        assert_eq!(serde_json::to_value(&snapshot).unwrap(), before);
        assert_eq!(serde_json::to_value(simulation.snapshot()).unwrap(), before);
    }

    #[test]
    fn browser_and_assistive_f_chords_never_request_a_camera_change() {
        for modifier in [
            KeyCode::ControlLeft,
            KeyCode::ControlRight,
            KeyCode::SuperLeft,
            KeyCode::SuperRight,
            KeyCode::AltLeft,
            KeyCode::AltRight,
        ] {
            for shift in [false, true] {
                let mut pressed = vec![KeyCode::KeyF, modifier];
                if shift {
                    pressed.push(KeyCode::ShiftLeft);
                }
                assert!(
                    frame_shortcut(&keys(&pressed), true, false, EntityId::Mars).is_none(),
                    "camera changed for {pressed:?}"
                );
            }
        }
    }

    #[test]
    fn follow_requires_active_nonmodal_input_and_a_fresh_f_press() {
        let mut keys = keys(&[KeyCode::KeyF]);
        for (active, blocked) in [(false, false), (false, true), (true, true)] {
            assert!(frame_shortcut(&keys, active, blocked, EntityId::Mars).is_none());
        }
        assert!(frame_shortcut(&keys, true, false, EntityId::Mars).is_some());
        keys.clear();
        assert!(keys.pressed(KeyCode::KeyF));
        assert!(frame_shortcut(&keys, true, false, EntityId::Mars).is_none());
        assert!(frame_shortcut(&ButtonInput::default(), true, false, EntityId::Mars).is_none());
    }

    #[test]
    fn shift_f_keeps_the_selected_object_framing_shortcut() {
        let snapshot = Simulation::new_default().snapshot();
        for shift in [KeyCode::ShiftLeft, KeyCode::ShiftRight] {
            for focus in [EntityId::Phobos, EntityId::Mars, EntityId::ReturnShip] {
                let mut rig = CameraRig {
                    mode: CameraMode::FreeFlight,
                    focus,
                    pan_offset: Vec3::ONE,
                    zoom: 3.0,
                    ..default()
                };
                let request =
                    frame_shortcut(&keys(&[shift, KeyCode::KeyF]), true, false, focus).unwrap();
                apply_view_request(&mut rig, request, &snapshot);
                assert_eq!(rig.mode, CameraMode::Orbit);
                assert_eq!(rig.focus, focus);
                assert_eq!(rig.pan_offset, Vec3::ZERO);
                assert_eq!(rig.zoom, 1.0);
                assert_eq!(
                    rig.distance_km,
                    base_distance_km(focus, &snapshot, CameraMode::Orbit)
                );
            }
        }
    }

    #[test]
    fn overview_requests_frame_phobos_and_mars_without_replacing_the_close_f_shortcut() {
        let simulation = Simulation::new_default();
        let snapshot = simulation.snapshot();
        let before = serde_json::to_value(&snapshot).unwrap();
        let mut expected = CameraRig::default();
        overview::frame(&mut expected, &snapshot);
        let mut rig = CameraRig {
            mode: CameraMode::FreeFlight,
            focus: EntityId::Mars,
            ..default()
        };
        apply_view_request(
            &mut rig,
            CameraRequest {
                mode: Some(CameraMode::Overview),
                ..default()
            },
            &snapshot,
        );
        assert_eq!(rig.mode, expected.mode);
        assert_eq!(rig.focus, EntityId::Phobos);
        assert_eq!(rig.yaw, expected.yaw);
        assert_eq!(rig.pitch, expected.pitch);
        assert_eq!(rig.distance_km, expected.distance_km);
        let request = frame_shortcut(&keys(&[KeyCode::KeyF]), true, false, rig.focus).unwrap();
        apply_view_request(&mut rig, request, &snapshot);
        assert_eq!(rig.mode, CameraMode::Orbit);
        assert_eq!(rig.focus, EntityId::Lander);
        assert_eq!(rig.distance_km, 0.12);
        assert_eq!(serde_json::to_value(simulation.snapshot()).unwrap(), before);
    }

    #[test]
    fn partial_view_requests_preserve_unrequested_camera_state() {
        let snapshot = Simulation::new_default().snapshot();
        let mut rig = CameraRig {
            mode: CameraMode::FreeFlight,
            focus: EntityId::Mars,
            pan_offset: Vec3::ONE,
            zoom: 2.0,
            ..default()
        };
        let angles = (rig.yaw, rig.pitch);
        apply_view_request(
            &mut rig,
            CameraRequest {
                focus: Some(EntityId::ReturnShip),
                ..default()
            },
            &snapshot,
        );
        assert_eq!(rig.mode, CameraMode::FreeFlight);
        assert_eq!(rig.focus, EntityId::ReturnShip);
        assert_eq!(rig.pan_offset, Vec3::ZERO);
        assert_eq!(rig.zoom, 2.0);
        assert_eq!(rig.distance_km, 0.5);
        assert_eq!((rig.yaw, rig.pitch), angles);
        apply_view_request(
            &mut rig,
            CameraRequest {
                mode: Some(CameraMode::FirstPerson),
                ..default()
            },
            &snapshot,
        );
        assert_eq!(rig.mode, CameraMode::FirstPerson);
        assert_eq!(rig.focus, EntityId::ReturnShip);
        assert_eq!(rig.zoom, 2.0);
        assert_eq!((rig.yaw, rig.pitch), (0.0, 0.0));
    }

    #[test]
    fn reframing_preserves_safe_world_angles_but_replaces_inward_or_invalid_angles() {
        let snapshot = Simulation::new_default().snapshot();
        let mut rig = CameraRig::default();
        frame_focus(&mut rig, EntityId::Lander, &snapshot);
        let angles = (rig.yaw, rig.pitch);
        frame_focus(&mut rig, EntityId::Lander, &snapshot);
        assert_eq!((rig.yaw, rig.pitch), angles);
        let target = focus_target(&rig, &snapshot);
        let outward = target.normalize();
        let inward = -outward;
        for (yaw, pitch) in [
            (inward.x.atan2(inward.z), inward.y.asin()),
            (f32::NAN, 0.0),
            (0.0, f32::NAN),
            (f32::INFINITY, 0.0),
            (0.0, f32::NEG_INFINITY),
            (0.0, 2.0),
        ] {
            rig.yaw = yaw;
            rig.pitch = pitch;
            frame_focus(&mut rig, EntityId::Lander, &snapshot);
            assert!(rig.yaw.is_finite() && rig.pitch.is_finite());
            assert!(orbit_offset(&rig, rig.distance_km).dot(outward) > 0.0);
            let view = orbit_view(&rig, &snapshot);
            close(view.translation, orbit_position(target, &rig));
            close(outside_body(view.translation), view.translation);
            close(outside_mars(view.translation, &snapshot), view.translation);
        }
    }

    #[test]
    fn orbit_tracks_each_real_snapshot_in_world_space_without_steering_the_lander() {
        let mut simulation = Simulation::new_default();
        let initial = simulation.snapshot();
        let mut rig = CameraRig::default();
        frame_focus(&mut rig, EntityId::Lander, &initial);
        let angles = (rig.yaw, rig.pitch);
        let offset = orbit_offset(&rig, rig.distance_km);
        let mut previous_time = initial.time_s;
        for _ in 0..12 {
            let snapshot = simulation
                .execute(crate::model::PhysicsCommand::Step { seconds: 0.1 })
                .unwrap();
            assert!(snapshot.time_s > previous_time);
            previous_time = snapshot.time_s;
            let lander = snapshot
                .assets
                .iter()
                .find(|asset| asset.id == EntityId::Lander)
                .unwrap();
            let position =
                Vec3::from_array(lander.position.map(|value| (value * KM as f64) as f32));
            close(focus_target(&rig, &snapshot), position);
            close(
                orbit_position(focus_target(&rig, &snapshot), &rig),
                position + offset,
            );
            let view = orbit_view(&rig, &snapshot);
            let expected_position = outside_mars(outside_body(position + offset), &snapshot);
            close(view.translation, expected_position);
            let expected =
                Transform::from_translation(expected_position).looking_at(position, Vec3::Y);
            close(view.rotation * Vec3::X, expected.rotation * Vec3::X);
            close(view.rotation * Vec3::Y, expected.rotation * Vec3::Y);
            close(view.rotation * Vec3::NEG_Z, expected.rotation * Vec3::NEG_Z);
            assert_eq!((rig.yaw, rig.pitch), angles);
            assert_eq!(lander.commanded_accel_mps2, [0.0; 3]);
            assert_eq!(lander.commanded_angular_accel_rad_s2, [0.0; 3]);

            rig.pan_offset = Vec3::new(0.01, -0.02, 0.03);
            close(focus_target(&rig, &snapshot), position + rig.pan_offset);
            rig.pan_offset = Vec3::ZERO;
        }
    }

    #[test]
    fn orbit_safeguards_keep_the_view_outside_real_terrain_and_mars() {
        let snapshot = Simulation::new_default().snapshot();
        let body_position = outside_body(Vec3::ZERO);
        let surface =
            crate::terrain::surface_radius_m(body_position.normalize().as_dvec3()) as f32 * KM;
        assert!(body_position.length() >= surface + 0.003 - 2e-6);
        close(outside_body(body_position), body_position);

        let mars_center = meters(snapshot.environment.mars_position_m);
        let mars_position = outside_mars(mars_center, &snapshot);
        let mars_bound = snapshot.environment.mars_radius_m as f32 * KM + 0.25;
        assert!(mars_position.distance(mars_center) >= mars_bound - 0.001);
        close(outside_mars(mars_position, &snapshot), mars_position);
    }
}
