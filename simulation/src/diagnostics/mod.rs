//! Removable, default-off observation of the rendered app's main-world transforms.
mod data;
mod systems;

#[cfg(all(test, not(target_arch = "wasm32")))]
mod tests;

use data::{Diagnostics, Measurement, PresentationSample, Report, State};
use std::cell::RefCell;

pub(crate) use systems::install;

thread_local! {
    static DIAGNOSTICS: RefCell<Diagnostics> = const { RefCell::new(Diagnostics::Disabled) };
}

pub(crate) fn set_enabled(enabled: bool) {
    DIAGNOSTICS.with(|state| {
        let mut state = state.borrow_mut();
        match (&*state, enabled) {
            (_, false) => *state = Diagnostics::Disabled,
            (Diagnostics::Disabled, true) => *state = Diagnostics::WaitingForScene,
            (Diagnostics::WaitingForScene | Diagnostics::Observed(_), true) => {}
        }
    });
}

fn enabled() -> bool {
    DIAGNOSTICS.with(|state| !matches!(&*state.borrow(), Diagnostics::Disabled))
}

fn publish(sample: PresentationSample) {
    DIAGNOSTICS.with(|state| {
        let mut state = state.borrow_mut();
        match &*state {
            Diagnostics::Disabled => {}
            Diagnostics::WaitingForScene | Diagnostics::Observed(_) => {
                *state = Diagnostics::Observed(Box::new(sample));
            }
        }
    });
}

fn scene_unavailable() {
    // A main-world resource can also be dropped during thread-local teardown.
    let _ = DIAGNOSTICS.try_with(|state| {
        if let Ok(mut state) = state.try_borrow_mut() {
            match &*state {
                Diagnostics::Disabled => {}
                Diagnostics::WaitingForScene | Diagnostics::Observed(_) => {
                    *state = Diagnostics::WaitingForScene;
                }
            }
        }
    });
}

/// Serialize only the last real collection. Reading never queries or ticks a world.
pub(crate) fn status() -> String {
    DIAGNOSTICS.with(|state| {
        let state = state.borrow();
        let (enabled, available, phase, sample) = match &*state {
            Diagnostics::Disabled => (false, false, State::Disabled, None),
            Diagnostics::WaitingForScene => (true, false, State::WaitingForScene, None),
            Diagnostics::Observed(sample) => (true, true, State::Observed, Some(sample.as_ref())),
        };
        serde_json::to_string(&Report {
            schema: "wsorb.presentation_diagnostics.v1",
            enabled,
            available,
            state: phase,
            measurement: Measurement::BevyMainWorldTransformsNotGpuPixels,
            sample,
        })
        .expect("presentation diagnostics are serializable")
    })
}
