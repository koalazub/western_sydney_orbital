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

use super::model::{FullscreenButton, HudState};

struct FullscreenTarget {
    node: ComputedNode,
    transform: UiGlobalTransform,
    clip: Option<CalculatedClip>,
    viewport: Rect,
    physical_to_css: f32,
    focused: bool,
}

impl FullscreenTarget {
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
}

thread_local! {
    static FULLSCREEN_TARGET: RefCell<Option<FullscreenTarget>> = const { RefCell::new(None) };
}

pub(super) fn install(app: &mut App) {
    FULLSCREEN_TARGET.with(|target| *target.borrow_mut() = None);
    app.add_systems(
        PostUpdate,
        register_fullscreen_button
            .after(UiSystems::PostLayout)
            .after(VisibilitySystems::VisibilityPropagate),
    );
}

pub(super) fn fullscreen_button_at(x: f32, y: f32) -> bool {
    FULLSCREEN_TARGET.with(|target| {
        target
            .borrow()
            .as_ref()
            .is_some_and(|target| target.contains(Vec2::new(x, y)))
    })
}

pub(super) fn fullscreen_button_focused() -> bool {
    FULLSCREEN_TARGET.with(|target| {
        target
            .borrow()
            .as_ref()
            .is_some_and(|target| target.focused)
    })
}

type FullscreenGeometry = (
    Entity,
    &'static Node,
    &'static ComputedNode,
    &'static UiGlobalTransform,
    &'static InheritedVisibility,
    &'static ComputedUiTargetCamera,
    Option<&'static CalculatedClip>,
    Has<InteractionDisabled>,
);

fn register_fullscreen_button(
    hud: Res<HudState>,
    input_focus: Res<InputFocus>,
    ui_scale: Res<UiScale>,
    buttons: Query<FullscreenGeometry, (With<FullscreenButton>, With<Button>)>,
    cameras: Query<(&Camera, &RenderTarget)>,
    windows: Query<Entity, With<PrimaryWindow>>,
) {
    let target = (|| {
        if hud.reset_confirm_open {
            return None;
        }
        let (entity, style, node, transform, visibility, target_camera, clip, disabled) =
            buttons.single().ok()?;
        if disabled
            || style.display == Display::None
            || !visibility.get()
            || node.is_empty()
            || !node.size().is_finite()
            || !transform.affine().is_finite()
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
        Some(FullscreenTarget {
            node: *node,
            transform: *transform,
            clip: clip.cloned(),
            viewport,
            physical_to_css,
            focused: input_focus.get() == Some(entity),
        })
    })();
    // Always replace it: hidden, disabled, removed or modal UI must not leave
    // a stale hit target that can still invoke a privileged browser action.
    FULLSCREEN_TARGET.with(|state| *state.borrow_mut() = target);
}
