use serde::{Deserialize, Deserializer, Serialize, Serializer};
use std::{fmt, str::FromStr};

#[derive(Clone, Copy, Debug, PartialEq, Eq, PartialOrd, Ord, Hash)]
pub enum EntityId {
    Lander,
    ReturnShip,
    LandingPad,
    Debris(u32),
    Phobos,
    Mars,
    L1,
    L2,
}

impl fmt::Display for EntityId {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Lander => formatter.write_str("Shuttle"),
            Self::ReturnShip => formatter.write_str("Target"),
            Self::LandingPad => formatter.write_str("LandingPad"),
            Self::Debris(index) => write!(formatter, "Debris{index}"),
            Self::Phobos => formatter.write_str("Phobos"),
            Self::Mars => formatter.write_str("Mars"),
            Self::L1 => formatter.write_str("L1"),
            Self::L2 => formatter.write_str("L2"),
        }
    }
}

impl FromStr for EntityId {
    type Err = String;

    fn from_str(value: &str) -> Result<Self, Self::Err> {
        match value {
            "Shuttle" | "Lander" => Ok(Self::Lander),
            "Target" => Ok(Self::ReturnShip),
            "LandingPad" => Ok(Self::LandingPad),
            "Phobos" => Ok(Self::Phobos),
            "Mars" => Ok(Self::Mars),
            "L1" => Ok(Self::L1),
            "L2" => Ok(Self::L2),
            name => name
                .strip_prefix("Debris")
                .filter(|index| {
                    !index.is_empty() && index.bytes().all(|byte| byte.is_ascii_digit())
                })
                .and_then(|index| index.parse::<u32>().ok())
                .filter(|index| *index > 0)
                .map(Self::Debris)
                .ok_or_else(|| format!("unknown entity {name}")),
        }
    }
}

impl Serialize for EntityId {
    fn serialize<S: Serializer>(&self, serializer: S) -> Result<S::Ok, S::Error> {
        serializer.collect_str(self)
    }
}

impl<'de> Deserialize<'de> for EntityId {
    fn deserialize<D: Deserializer<'de>>(deserializer: D) -> Result<Self, D::Error> {
        String::deserialize(deserializer)?
            .parse()
            .map_err(serde::de::Error::custom)
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub enum EntityKind {
    #[serde(rename = "ship")]
    Lander,
    #[serde(rename = "target")]
    ReturnShip,
    #[serde(rename = "landing_pad")]
    LandingPad,
    #[serde(rename = "asteroid")]
    Debris,
}

impl EntityKind {
    pub const fn is_spacecraft(self) -> bool {
        matches!(self, Self::Lander | Self::ReturnShip)
    }
}

impl fmt::Display for EntityKind {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(match self {
            Self::Lander => "Lander",
            Self::ReturnShip => "Return ship",
            Self::LandingPad => "Landing pad",
            Self::Debris => "Debris",
        })
    }
}

#[derive(Clone, Copy, Debug, Default, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum EntityStatus {
    Landed,
    #[default]
    Coasting,
    Thrusting,
    Rotating,
    Fixed,
    StationKeeping,
}

impl fmt::Display for EntityStatus {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(match self {
            Self::Landed => "Landed",
            Self::Coasting => "Coasting",
            Self::Thrusting => "Thrusting",
            Self::Rotating => "Rotating",
            Self::Fixed => "Fixed",
            Self::StationKeeping => "Station keeping",
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn debris_ids_round_trip_beyond_u16_without_changing_old_names() {
        for index in [1, 500, 1_000, 65_535, 65_536, 100_000, u32::MAX] {
            let id = EntityId::Debris(index);
            let text = format!("Debris{index}");
            assert_eq!(id.to_string(), text);
            assert_eq!(text.parse::<EntityId>().unwrap(), id);
            assert_eq!(serde_json::to_value(id).unwrap(), text);
            assert_eq!(
                serde_json::from_value::<EntityId>(serde_json::json!(text)).unwrap(),
                id
            );
        }
        assert_eq!("Lander".parse::<EntityId>().unwrap(), EntityId::Lander);
        assert_eq!(EntityId::Lander.to_string(), "Shuttle");
    }

    #[test]
    fn malformed_and_overflowing_debris_ids_are_rejected() {
        for text in [
            "Debris",
            "Debris0",
            "Debris000",
            "Debris-1",
            "Debris+1",
            "Debris1.0",
            "Debris1e3",
            "Debris 1",
            "Debris1 ",
            " Debris1",
            "debris1",
            "Debris4294967296",
            "Debris18446744073709551616",
        ] {
            assert!(text.parse::<EntityId>().is_err(), "{text}");
            assert!(
                serde_json::from_value::<EntityId>(serde_json::json!(text)).is_err(),
                "{text}"
            );
        }
        assert!(serde_json::from_str::<EntityId>("100000").is_err());
    }
}
