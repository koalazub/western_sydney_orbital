//! The bundled monospace atlas is ASCII-only. Keep native units readable while
//! retaining the original SI symbols in machine-readable telemetry.

pub(crate) fn readable(value: &str) -> String {
    let value = value.replace("N·m", "N m");
    let mut output = String::with_capacity(value.len());
    for character in value.chars() {
        match character {
            '²' => output.push_str("^2"),
            '³' => output.push_str("^3"),
            '×' => output.push('x'),
            '·' => output.push('|'),
            '−' | '–' | '—' => output.push('-'),
            '±' => output.push_str("+/-"),
            '°' => output.push_str(" deg"),
            'µ' => output.push('u'),
            _ => output.push(character),
        }
    }
    output
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn physics_symbols_have_readable_native_font_equivalents() {
        assert_eq!(
            readable("1.5 × · 0.5 m/s² · 20° · ±Z"),
            "1.5 x | 0.5 m/s^2 | 20 deg | +/-Z"
        );
    }
}
