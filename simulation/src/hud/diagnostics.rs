//! Read-only native UI geometry for diagnosing a missing HUD without browser automation.
use super::model::{HudAction, HudRoot, Readout};
use bevy::{
    camera::{RenderTarget, visibility::VisibilitySystems},
    prelude::*,
    text::TextLayoutInfo,
    ui::UiSystems,
    window::PrimaryWindow,
};
use serde::Serialize;
use std::cell::RefCell;

#[derive(Default, Serialize)]
struct Report {
    samples: u64,
    viewport_css: Option<[f32; 2]>,
    native_nodes: usize,
    elements: Vec<Element>,
}

#[derive(Serialize)]
struct Element {
    role: &'static str,
    displayed: bool,
    inherited_visible: bool,
    rect_css: Option<[f32; 4]>,
    target_camera_active: Option<bool>,
    targets_window: bool,
    glyphs: Option<usize>,
}

thread_local! { static REPORT: RefCell<Report> = RefCell::new(Report::default()); }

pub(super) fn install(app: &mut App) {
    app.add_systems(
        PostUpdate,
        record
            .after(UiSystems::PostLayout)
            .after(VisibilitySystems::VisibilityPropagate),
    );
}

pub(super) fn status() -> String {
    REPORT.with(|report| serde_json::to_string(&*report.borrow()).expect("HUD geometry is finite"))
}

type Geometry = (
    &'static Node,
    &'static ComputedNode,
    &'static UiGlobalTransform,
    &'static InheritedVisibility,
    &'static ComputedUiTargetCamera,
    Option<&'static Readout>,
    Option<&'static HudAction>,
    Option<&'static TextLayoutInfo>,
    Has<HudRoot>,
);

fn record(
    ui_scale: Res<UiScale>,
    windows: Query<&Window, With<PrimaryWindow>>,
    cameras: Query<(&Camera, &RenderTarget)>,
    nodes: Query<Geometry>,
) {
    let viewport_css = windows.single().ok().and_then(|window| {
        let size = [window.width(), window.height()];
        size.iter().all(|v| v.is_finite()).then_some(size)
    });
    REPORT.with(|report| {
        let mut report = report.borrow_mut();
        report.samples = report.samples.saturating_add(1);
        report.viewport_css = viewport_css;
        report.native_nodes = nodes.iter().len();
        report.elements.clear();
        for (node, computed, transform, visible, target, readout, action, text, root) in &nodes {
            let role = if root {
                Some("root")
            } else {
                match (readout, action) {
                    (Some(Readout::TelemetryBrief), _) => Some("flight_metrics"),
                    (Some(Readout::Summary), _) => Some("mission_summary"),
                    (Some(Readout::InputSettings), _) => Some("input_settings"),
                    (_, Some(HudAction::TogglePlayback)) => Some("run_pause"),
                    (_, Some(HudAction::ToggleDetails)) => Some("details"),
                    (_, Some(HudAction::ToggleControls)) => Some("controls_help"),
                    (_, Some(HudAction::FollowLander)) => Some("follow_lander"),
                    (_, Some(HudAction::DebugUpright)) => Some("debug_upright"),
                    _ => None,
                }
            };
            let Some(role) = role else {
                continue;
            };
            let half = computed.size() * 0.5;
            let affine = transform.affine();
            let physical_to_css = computed.inverse_scale_factor() * ui_scale.0;
            let min = affine.transform_point2(-half) * physical_to_css;
            let max = affine.transform_point2(half) * physical_to_css;
            let rect = [min.x, min.y, max.x - min.x, max.y - min.y];
            let camera = target.get().and_then(|target| cameras.get(target).ok());
            report.elements.push(Element {
                role,
                displayed: node.display != Display::None,
                inherited_visible: visible.get(),
                rect_css: rect.iter().all(|v| v.is_finite()).then_some(rect),
                target_camera_active: camera.map(|(camera, _)| camera.is_active),
                targets_window: camera
                    .is_some_and(|(_, target)| matches!(target, RenderTarget::Window(_))),
                glyphs: text.map(|layout| layout.glyphs.len()),
            });
        }
    });
}
