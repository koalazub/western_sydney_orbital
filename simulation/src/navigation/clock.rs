//! Receipt-time adapter for notebook presence only, never simulation timestamps.

#[cfg(not(target_arch = "wasm32"))]
pub(super) fn now_s() -> f64 {
    static START: std::sync::OnceLock<std::time::Instant> = std::sync::OnceLock::new();
    START
        .get_or_init(std::time::Instant::now)
        .elapsed()
        .as_secs_f64()
}

#[cfg(target_arch = "wasm32")]
pub(super) fn now_s() -> f64 {
    use wasm_bindgen::{JsCast, JsValue};
    let performance = js_sys::Reflect::get(&js_sys::global(), &JsValue::from_str("performance"));
    if let Ok(performance) = performance
        && let Ok(now) = js_sys::Reflect::get(&performance, &JsValue::from_str("now"))
        && let Some(now) = now.dyn_ref::<js_sys::Function>()
        && let Ok(value) = now.call0(&performance)
        && let Some(milliseconds) = value.as_f64()
        && milliseconds.is_finite()
    {
        return milliseconds * 0.001;
    }
    js_sys::Date::now() * 0.001
}
