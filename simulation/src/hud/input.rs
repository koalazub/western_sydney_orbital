//! Geometry for browser actions that must run during trusted DOM input, before
//! the next Bevy frame. This records native UI state and makes no browser calls.

use std::cell::RefCell;

use bevy::{
    camera::{RenderTarget, visibility::VisibilitySystems},
    input_focus::InputFocus,
    prelude::*,
    ui::{InteractionDisabled, UiSystems},
    ui_widgets::Button,
    window::{PrimaryWindow, WindowRef},
};

use super::model::{HudState, PlatformButton};

struct HitTarget {
    node: ComputedNode,
    transform: UiGlobalTransform,
    clip: Option<CalculatedClip>,
    viewport: Rect,
    physical_to_css: f32,
    focused: bool,
}

impl HitTarget {
    fn contains(&self, css_point: Vec2) -> bool {
        if !css_point.is_finite() {
            return false;
        }
        let physical_point = css_point / self.physical_to_css;
        if !self.viewport.contains(physical_point) {
            return false;
        }
        // UI transforms are viewport-local, with the node's center at zero.
        // Use Bevy's rounded-node and inherited-clip tests, just as picking does.
        let point = physical_point - self.viewport.min;
        self.node.contains_point(self.transform, point)
            && self
                .clip
                .as_ref()
                .is_none_or(|clip| clip.contains_point(point))
    }

    fn fully_visible(&self) -> bool {
        let bounds = self.node.border_box();
        [
            bounds.min,
            Vec2::new(bounds.max.x, bounds.min.y),
            bounds.max,
            Vec2::new(bounds.min.x, bounds.max.y),
        ]
        .into_iter()
        .map(|point| self.transform.transform_point2(point))
        .all(|point| {
            self.viewport.contains(point + self.viewport.min)
                && self
                    .clip
                    .as_ref()
                    .is_none_or(|clip| clip.contains_point(point))
        })
    }
}

thread_local! {
    static TARGETS: RefCell<[Option<HitTarget>; 2]> = const { RefCell::new([None, None]) };
}

pub(super) fn install(app: &mut App) {
    TARGETS.with(|targets| *targets.borrow_mut() = [None, None]);
    app.add_systems(
        PostUpdate,
        register_platform_buttons
            .after(UiSystems::PostLayout)
            .after(VisibilitySystems::VisibilityPropagate),
    );
}

pub(super) fn fullscreen_button_at(x: f32, y: f32) -> bool {
    button_at(PlatformButton::Fullscreen, x, y)
}

pub(super) fn fullscreen_button_focused() -> bool {
    button_focused(PlatformButton::Fullscreen)
}

pub(super) fn notebook_button_at(x: f32, y: f32) -> bool {
    button_at(PlatformButton::Notebook, x, y)
}

pub(super) fn notebook_button_focused() -> bool {
    button_focused(PlatformButton::Notebook)
}

fn button_at(button: PlatformButton, x: f32, y: f32) -> bool {
    !crate::controls::binding_actions_blocked()
        && TARGETS.with(|targets| {
            targets.borrow()[button.index()]
                .as_ref()
                .is_some_and(|target| target.contains(Vec2::new(x, y)))
        })
}

fn button_focused(button: PlatformButton) -> bool {
    !crate::controls::binding_actions_blocked()
        && TARGETS.with(|targets| {
            targets.borrow()[button.index()]
                .as_ref()
                .is_some_and(|target| target.focused)
        })
}

type PlatformGeometry = (
    Entity,
    &'static PlatformButton,
    &'static Node,
    &'static ComputedNode,
    &'static UiGlobalTransform,
    &'static InheritedVisibility,
    &'static ComputedUiTargetCamera,
    Option<&'static CalculatedClip>,
    Has<InteractionDisabled>,
);

fn register_platform_buttons(
    hud: Res<HudState>,
    input_focus: Res<InputFocus>,
    ui_scale: Res<UiScale>,
    buttons: Query<PlatformGeometry, With<Button>>,
    cameras: Query<(&Camera, &RenderTarget)>,
    windows: Query<Entity, With<PrimaryWindow>>,
) {
    let mut targets = [None, None];
    let mut seen = [false; 2];
    if !hud.reset_confirm_open && !crate::controls::binding_actions_blocked() {
        for (entity, button, style, node, transform, visibility, target_camera, clip, disabled) in
            &buttons
        {
            let index = button.index();
            if seen[index] {
                // Duplicate markers are ambiguous, so neither may activate.
                targets[index] = None;
                continue;
            }
            seen[index] = true;
            targets[index] = (|| {
                if disabled
                    || style.display == Display::None
                    || !visibility.get()
                    || node.is_empty()
                    || !node.size().is_finite()
                    || !transform.affine().is_finite()
                    || transform.try_inverse().is_none()
                    || clip.is_some_and(CalculatedClip::is_fully_clipped)
                {
                    return None;
                }
                let (camera, render_target) = cameras.get(target_camera.get()?).ok()?;
                let primary_window = windows.single().ok()?;
                let on_canvas = match render_target {
                    RenderTarget::Window(WindowRef::Primary) => true,
                    RenderTarget::Window(WindowRef::Entity(window)) => *window == primary_window,
                    _ => false,
                };
                if !camera.is_active || !on_canvas {
                    return None;
                }
                // ComputedNode includes both device-pixel ratio and UiScale. Canvas
                // CSS coordinates include the former, but must not undo UI scaling.
                let physical_to_css = node.inverse_scale_factor() * ui_scale.0;
                if !physical_to_css.is_finite() || physical_to_css <= 0.0 {
                    return None;
                }
                let viewport = camera.physical_viewport_rect()?.as_rect();
                if viewport.is_empty() {
                    return None;
                }
                let target = HitTarget {
                    node: *node,
                    transform: *transform,
                    clip: clip.cloned(),
                    viewport,
                    physical_to_css,
                    focused: input_focus.get() == Some(entity),
                };
                target.fully_visible().then_some(target)
            })();
        }
    }
    // Always replace it: hidden, disabled, removed or modal UI must not leave
    // a stale hit target that can still invoke a privileged browser action.
    TARGETS.with(|state| *state.borrow_mut() = targets);
}
