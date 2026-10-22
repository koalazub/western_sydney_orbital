use super::model::{CameraError, CameraRig, CameraStatus};
use crate::{
    domain::{CameraMode, CameraRequest, EngineAction, EntityId, EntityKind, ViewportAction},
    model::Snapshot,
};
use bevy::{
    ecs::system::SystemParam,
    input::mouse::{AccumulatedMouseMotion, AccumulatedMouseScroll, MouseScrollUnit},
    input_focus::InputFocus,
    picking::hover::Hovered,
    prelude::*,
    ui::Pressed,
    ui_widgets::Button as UiButton,
};
use std::{cell::RefCell, collections::VecDeque};

const KM: f32 = 0.001;
const MIN_DISTANCE_KM: f32 = 0.025;
const MAX_DISTANCE_KM: f32 = 20_000.0;

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
}

pub(super) fn install(app: &mut App) {
    app.init_resource::<CameraRig>()
        .add_systems(Update, update_camera.in_set(crate::EngineSet::Camera));
}

pub(super) fn set_input_active(active: bool, locked: bool) {
    GATE.with(|gate| *gate.borrow_mut() = InputGate { active, locked });
}

pub(super) fn input_active() -> bool {
    GATE.with(|gate| gate.borrow().active)
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

#[derive(SystemParam)]
struct CameraInput<'w, 's> {
    time: Res<'w, Time>,
    keys: Res<'w, ButtonInput<KeyCode>>,
    buttons: Res<'w, ButtonInput<MouseButton>>,
    input_focus: Res<'w, InputFocus>,
    motion: Res<'w, AccumulatedMouseMotion>,
    scroll: Res<'w, AccumulatedMouseScroll>,
    windows: Query<'w, 's, &'static Window>,
    ui: Query<'w, 's, (Entity, &'static Hovered, Has<Pressed>), With<UiButton>>,
}

fn update_camera(
    input: CameraInput,
    mut cameras: Query<(&Camera, &GlobalTransform, &mut Transform), With<SimulationCamera>>,
    mut rig: ResMut<CameraRig>,
    mut gesture: Local<PointerGesture>,
) {
    let CameraInput {
        time,
        keys,
        buttons,
        input_focus,
        motion,
        scroll,
        windows,
        ui,
    } = input;
    let mut focus_changed = false;
    let mut mode_changed = false;
    let mut zoom_changed = false;
    while let Some(request) = COMMANDS.with(|queue| queue.borrow_mut().pop_front()) {
        if let Some(mode) = request.mode {
            mode_changed |= rig.mode != mode;
            rig.mode = mode;
            if mode_changed && mode == CameraMode::FirstPerson {
                rig.yaw = 0.0;
                rig.pitch = 0.0;
            }
        }
        if let Some(focus) = request.focus {
            focus_changed |= rig.focus != focus;
            rig.focus = focus;
            rig.pan_offset = Vec3::ZERO;
        }
        if let Some(zoom) = request.zoom {
            rig.zoom = zoom;
            zoom_changed = true;
        }
    }

    let gate = GATE.with(|gate| *gate.borrow());
    if !gate.active {
        gesture.start = None;
        gesture.dragged = false;
        gesture.blocked = false;
    }

    let Ok((camera, global, mut transform)) = cameras.single_mut() else {
        return;
    };
    if mode_changed && rig.mode == CameraMode::FreeFlight {
        preserve_pose(&mut rig, transform.rotation);
    }
    let snapshot = crate::snapshot();
    if !focus_exists(rig.focus, &snapshot) {
        rig.focus = EntityId::Lander;
        rig.pan_offset = Vec3::ZERO;
        focus_changed = true;
    }
    if focus_changed || zoom_changed || mode_changed {
        rig.distance_km = (base_distance_km(rig.focus, &snapshot, rig.mode) * rig.zoom)
            .clamp(MIN_DISTANCE_KM, MAX_DISTANCE_KM);
    }
    let mut target = focus_position(rig.focus, &snapshot) + rig.pan_offset;
    if !rig.initialized {
        let forward = transform.rotation * Vec3::NEG_Z;
        rig.yaw = (-forward.x).atan2(-forward.z);
        rig.pitch = forward.y.asin().clamp(-1.52, 1.52);
        rig.distance_km = base_distance_km(rig.focus, &snapshot, rig.mode) * rig.zoom;
        rig.initialized = true;
    }

    let over_ui = ui
        .iter()
        .any(|(_, hovered, pressed)| hovered.get() || pressed);
    let button_focused = input_focus.get().is_some_and(|entity| ui.contains(entity));
    let cursor = windows.single().ok().and_then(Window::cursor_position);
    let dt = time.delta_secs().min(0.1);
    if gate.active && !button_focused {
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
            preserve_pose(&mut rig, transform.rotation);
            rig.mode = CameraMode::FreeFlight;
        }
        if keys.just_pressed(KeyCode::Digit5) {
            rig.mode = CameraMode::Overview;
            rig.distance_km = base_distance_km(rig.focus, &snapshot, rig.mode);
        }
        if keys.just_pressed(KeyCode::KeyF) {
            rig.pan_offset = Vec3::ZERO;
            rig.mode = CameraMode::Orbit;
            rig.zoom = 1.0;
            rig.distance_km = base_distance_km(rig.focus, &snapshot, rig.mode);
        }
        if !button_focused && keys.pressed(KeyCode::ArrowLeft) {
            rig.yaw += dt;
        }
        if !button_focused && keys.pressed(KeyCode::ArrowRight) {
            rig.yaw -= dt;
        }
        if !button_focused && keys.pressed(KeyCode::ArrowUp) {
            rig.pitch = (rig.pitch + dt).clamp(-1.52, 1.52);
        }
        if !button_focused && keys.pressed(KeyCode::ArrowDown) {
            rig.pitch = (rig.pitch - dt).clamp(-1.52, 1.52);
        }
    }

    if gate.active {
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
                    if !gesture.dragged && !gesture.blocked && !over_ui {
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

        if !over_ui && scroll.delta.y != 0.0 {
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

        let moving = !button_focused
            && [
                KeyCode::KeyW,
                KeyCode::KeyA,
                KeyCode::KeyS,
                KeyCode::KeyD,
                KeyCode::KeyQ,
                KeyCode::KeyE,
            ]
            .into_iter()
            .any(|key| keys.pressed(key));
        if moving {
            if rig.mode != CameraMode::FreeFlight {
                preserve_pose(&mut rig, transform.rotation);
                rig.mode = CameraMode::FreeFlight;
            }
            transform.rotation = look_rotation(rig.yaw, rig.pitch);
            let forward = transform.rotation * Vec3::NEG_Z;
            let right = transform.rotation * Vec3::X;
            let mut direction = Vec3::ZERO;
            if keys.pressed(KeyCode::KeyW) {
                direction += forward;
            }
            if keys.pressed(KeyCode::KeyS) {
                direction -= forward;
            }
            if keys.pressed(KeyCode::KeyD) {
                direction += right;
            }
            if keys.pressed(KeyCode::KeyA) {
                direction -= right;
            }
            if keys.pressed(KeyCode::KeyE) {
                direction += Vec3::Y;
            }
            if keys.pressed(KeyCode::KeyQ) {
                direction -= Vec3::Y;
            }
            let boost = if shift { 5.0 } else { 1.0 };
            transform.translation +=
                direction.normalize_or_zero() * rig.speed_mps * KM * dt * boost;
        }
    }

    target = focus_position(rig.focus, &snapshot) + rig.pan_offset;
    match rig.mode {
        CameraMode::FreeFlight => transform.rotation = look_rotation(rig.yaw, rig.pitch),
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
            let center = rig.pan_offset;
            transform.translation = outside_body(orbit_position(center, &rig));
            transform.look_at(center, Vec3::Y);
        }
        CameraMode::Orbit => {
            transform.translation = orbit_position(target, &rig);
            transform.translation = outside_body(transform.translation);
            transform.translation = outside_mars(transform.translation, &snapshot);
            transform.look_at(target, Vec3::Y);
        }
    }
    STATUS.with(|status| {
        let next = CameraStatus::from_view(
            &rig,
            &transform,
            focus_position(rig.focus, &snapshot),
            gate.active,
            gate.locked,
        );
        *status.borrow_mut() = next;
    });
}

fn preserve_pose(rig: &mut CameraRig, rotation: Quat) {
    let forward = rotation * Vec3::NEG_Z;
    rig.yaw = (-forward.x).atan2(-forward.z);
    rig.pitch = forward.y.asin().clamp(-1.52, 1.52);
}

fn rotate_camera(rig: &mut CameraRig, delta: Vec2) {
    rig.yaw -= delta.x * 0.004;
    rig.pitch = (rig.pitch + delta.y * 0.004).clamp(-1.52, 1.52);
}

fn look_rotation(yaw: f32, pitch: f32) -> Quat {
    Transform::IDENTITY
        .looking_to(
            Vec3::new(
                -yaw.sin() * pitch.cos(),
                pitch.sin(),
                -yaw.cos() * pitch.cos(),
            ),
            Vec3::Y,
        )
        .rotation
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

fn base_distance_km(focus: EntityId, snapshot: &Snapshot, mode: CameraMode) -> f32 {
    if mode == CameraMode::Overview {
        return 45.0;
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

fn outside_body(position: Vec3) -> Vec3 {
    let radius_bound = crate::model::phobos_radii_m()
        .into_iter()
        .fold(0.0_f64, f64::max) as f32
        * KM
        * 1.1;
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
