use serde::{Deserialize, Serialize};

#[derive(Clone, Copy, Debug, Default, PartialEq, Eq, Serialize, Deserialize)]
pub enum PlaybackRate {
    #[default]
    RealTime,
    Twofold,
    Fourfold,
    Fivefold,
    Tenfold,
}

impl PlaybackRate {
    pub const fn factor(self) -> f64 {
        match self {
            Self::RealTime => 1.0,
            Self::Twofold => 2.0,
            Self::Fourfold => 4.0,
            Self::Fivefold => 5.0,
            Self::Tenfold => 10.0,
        }
    }

    pub const fn next(self) -> Self {
        match self {
            Self::RealTime => Self::Fivefold,
            Self::Fivefold => Self::Tenfold,
            Self::Tenfold => Self::RealTime,
            Self::Twofold | Self::Fourfold => Self::RealTime,
        }
    }
}

impl TryFrom<f64> for PlaybackRate {
    type Error = &'static str;

    fn try_from(value: f64) -> Result<Self, Self::Error> {
        match value {
            1.0 => Ok(Self::RealTime),
            2.0 => Ok(Self::Twofold),
            4.0 => Ok(Self::Fourfold),
            5.0 => Ok(Self::Fivefold),
            10.0 => Ok(Self::Tenfold),
            _ => Err("play rate must be 1, 2, 4, 5 or 10"),
        }
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum ViewportAction {
    Fullscreen,
    Maximize,
    Capture,
    Release,
    Export,
    Notebook,
}

#[derive(Clone, Copy, Debug)]
pub enum EngineAction {
    TogglePlayback,
    Play(PlaybackRate),
    Pause,
    Step,
    Reset,
    DebugUpright,
    CycleRate,
    Viewport(ViewportAction),
}

#[cfg(test)]
mod tests {
    use super::PlaybackRate;

    #[test]
    fn playback_rates_include_two_and_four() {
        for (value, factor) in [(1.0, 1.0), (2.0, 2.0), (4.0, 4.0), (5.0, 5.0), (10.0, 10.0)] {
            assert_eq!(PlaybackRate::try_from(value).unwrap().factor(), factor);
        }
        assert!(PlaybackRate::try_from(3.0).is_err());
    }
}
