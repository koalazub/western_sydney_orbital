#[cfg(target_arch = "wasm32")]
use wasm_bindgen::JsCast;
use wasm_bindgen::JsValue;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
#[cfg_attr(
    not(target_arch = "wasm32"),
    allow(dead_code, reason = "Browser surface checks only run on wasm32.")
)]
pub(super) enum StartupError {
    DocumentUnavailable,
    InsecureContext,
    WebGpuUnavailable,
    CanvasMissing,
    CanvasInvalid,
    CanvasDetached,
    CanvasOwned,
    CanvasContextUnavailable,
    CanvasContextError,
    RendererAlreadyStarted,
}

impl StartupError {
    fn code(self) -> &'static str {
        match self {
            Self::DocumentUnavailable => "document_unavailable",
            Self::InsecureContext => "insecure_context",
            Self::WebGpuUnavailable => "webgpu_unavailable",
            Self::CanvasMissing => "canvas_missing",
            Self::CanvasInvalid => "canvas_invalid",
            Self::CanvasDetached => "canvas_detached",
            Self::CanvasOwned => "canvas_owned",
            Self::CanvasContextUnavailable => "canvas_context_unavailable",
            Self::CanvasContextError => "canvas_context_error",
            Self::RendererAlreadyStarted => "renderer_already_started",
        }
    }

    fn message(self) -> &'static str {
        match self {
            Self::DocumentUnavailable => {
                "Renderer startup requires a browser window with an active document."
            }
            Self::InsecureContext => {
                "WebGPU requires a secure browser context. Open the page over HTTPS or localhost."
            }
            Self::WebGpuUnavailable => {
                "This browser does not expose navigator.gpu. WebGPU is required to start the renderer."
            }
            Self::CanvasMissing => "No element matches the simulation canvas selector.",
            Self::CanvasInvalid => {
                "The simulation canvas selector must be valid CSS and select an HTML canvas element."
            }
            Self::CanvasDetached => {
                "The simulation canvas is not connected to the active browser document."
            }
            Self::CanvasOwned => {
                "The simulation canvas already has a renderer owner or native window handle. Reload to retry."
            }
            Self::CanvasContextUnavailable => {
                "The simulation canvas did not provide a WebGPU context. WebGPU may be unavailable, or the canvas may use another context type."
            }
            Self::CanvasContextError => {
                "The browser threw an exception while preparing the simulation canvas for WebGPU. Reload to retry."
            }
            Self::RendererAlreadyStarted => "Renderer already started. Reload to retry.",
        }
    }

    pub(super) fn into_js(self) -> JsValue {
        let error = js_sys::Error::new(self.message());
        error.set_name("WSOStartupError");
        let _ = js_sys::Reflect::set(error.as_ref(), &"code".into(), &self.code().into());
        error.into()
    }
}

#[cfg(any(target_arch = "wasm32", test))]
fn validate_ownership(
    connected: bool,
    has_renderer_owner: bool,
    has_raw_handle: bool,
) -> Result<(), StartupError> {
    if !connected {
        return Err(StartupError::CanvasDetached);
    }
    if has_renderer_owner || has_raw_handle {
        return Err(StartupError::CanvasOwned);
    }
    Ok(())
}

#[cfg(target_arch = "wasm32")]
pub(super) fn claim_for_bevy(canvas_selector: &str) -> Result<(), StartupError> {
    const OWNER_ATTRIBUTE: &str = "data-wsorb-renderer-owner";

    let window = web_sys::window().ok_or(StartupError::DocumentUnavailable)?;
    let document = window.document().ok_or(StartupError::DocumentUnavailable)?;
    if !window.is_secure_context() {
        return Err(StartupError::InsecureContext);
    }
    let canvas = document
        .query_selector(canvas_selector)
        .map_err(|_| StartupError::CanvasInvalid)?
        .ok_or(StartupError::CanvasMissing)?
        .dyn_into::<web_sys::HtmlCanvasElement>()
        .map_err(|_| StartupError::CanvasInvalid)?;
    validate_ownership(
        canvas.is_connected() && canvas.owner_document().as_ref() == Some(&document),
        canvas.has_attribute(OWNER_ATTRIBUTE),
        canvas.has_attribute("data-raw-handle"),
    )?;

    // Reflect avoids extra web-sys Navigator/GPU features. This is only a
    // capability check; Bevy still requests and owns the actual GPU device.
    let navigator = js_sys::Reflect::get(window.as_ref(), &"navigator".into())
        .map_err(|_| StartupError::WebGpuUnavailable)?;
    let gpu = js_sys::Reflect::get(&navigator, &"gpu".into())
        .map_err(|_| StartupError::WebGpuUnavailable)?;
    if gpu.is_null() || gpu.is_undefined() {
        return Err(StartupError::WebGpuUnavailable);
    }

    // Repeating this same context mode returns the same object. A successful
    // browser-loader preflight is valid, not evidence of native ownership.
    // Do not configure the context or request a device here.
    canvas
        .get_context("webgpu")
        .map_err(|_| StartupError::CanvasContextError)?
        .ok_or(StartupError::CanvasContextUnavailable)?;

    // Keep ownership after any later initialization failure. A second Bevy app
    // is not a recovery path; only an explicit page reload can retry then.
    canvas
        .set_attribute(OWNER_ATTRIBUTE, "bevy")
        .map_err(|_| StartupError::CanvasContextError)?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn startup_error_codes_are_stable_and_have_messages() {
        let errors = [
            (StartupError::DocumentUnavailable, "document_unavailable"),
            (StartupError::InsecureContext, "insecure_context"),
            (StartupError::WebGpuUnavailable, "webgpu_unavailable"),
            (StartupError::CanvasMissing, "canvas_missing"),
            (StartupError::CanvasInvalid, "canvas_invalid"),
            (StartupError::CanvasDetached, "canvas_detached"),
            (StartupError::CanvasOwned, "canvas_owned"),
            (
                StartupError::CanvasContextUnavailable,
                "canvas_context_unavailable",
            ),
            (StartupError::CanvasContextError, "canvas_context_error"),
            (
                StartupError::RendererAlreadyStarted,
                "renderer_already_started",
            ),
        ];
        for (error, code) in errors {
            assert_eq!(error.code(), code);
            assert!(!error.message().is_empty());
        }
    }

    #[test]
    fn disconnected_canvas_is_rejected_before_ownership() {
        assert_eq!(
            validate_ownership(false, true, true),
            Err(StartupError::CanvasDetached)
        );
    }

    #[test]
    fn either_native_marker_rejects_ownership_even_if_the_other_is_absent() {
        for (owner, raw_handle) in [(true, false), (false, true), (true, true)] {
            assert_eq!(
                validate_ownership(true, owner, raw_handle),
                Err(StartupError::CanvasOwned)
            );
        }
        // This only checks ownership flags, not WebGPU or renderer readiness.
        assert_eq!(validate_ownership(true, false, false), Ok(()));
    }
}
