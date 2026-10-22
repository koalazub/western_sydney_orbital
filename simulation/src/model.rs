use serde::{Deserialize, Serialize};

/// Approximate dimensions, not an official STP terrain model. The launch-side
/// depression is synthetic because the installed simulator's mesh/frame
/// transform is not published in the competition configuration.
pub const PHOBOS_RADII_M: [f64; 3] = [13_000.0, 11_400.0, 9_100.0];

/// Shared meter-scale terrain for Bevy and Rapier. No upstream assets are copied.
pub fn phobos_geometry() -> (Vec<[f64; 3]>, Vec<[u32; 3]>) {
    const LATITUDES: usize = 32;
    const LONGITUDES: usize = 64;
    let craters = [
        ([-0.078, 0.011, -0.996], 0.34, 0.15),
        ([0.35, 0.81, -0.46], 0.14, 0.12),
        ([0.78, -0.32, -0.53], 0.10, 0.09),
        ([0.25, 0.35, 0.9], 0.24, 0.08),
    ];
    let mut vertices = Vec::with_capacity((LATITUDES + 1) * (LONGITUDES + 1));
    for latitude in 0..=LATITUDES {
        let lat = -std::f64::consts::FRAC_PI_2
            + std::f64::consts::PI * latitude as f64 / LATITUDES as f64;
        for longitude in 0..=LONGITUDES {
            let lon = std::f64::consts::TAU * longitude as f64 / LONGITUDES as f64;
            let direction = [lat.cos() * lon.cos(), lat.sin(), lat.cos() * lon.sin()];
            let mut relief = 0.006 * (7.0 * lon + 0.3).sin() * (5.0 * lat).cos();
            for (center, width, depth) in craters {
                let center_length = f64::sqrt(center.iter().map(|x| x * x).sum());
                let cosine = direction
                    .iter()
                    .zip(center)
                    .map(|(a, b)| a * b / center_length)
                    .sum::<f64>();
                let angle = cosine.clamp(-1.0, 1.0).acos();
                let q = angle / width;
                let bowl = (1.0 - q * q).max(0.0);
                let rim = (-((q - 0.88) / 0.11).powi(2)).exp() * 0.025;
                relief += depth * bowl - rim;
            }
            vertices.push(std::array::from_fn(|i| {
                direction[i] * PHOBOS_RADII_M[i] * (1.0 - relief)
            }));
        }
    }
    let mut triangles = Vec::with_capacity(2 * LATITUDES * LONGITUDES);
    for latitude in 0..LATITUDES {
        for longitude in 0..LONGITUDES {
            let a = (latitude * (LONGITUDES + 1) + longitude) as u32;
            let b = a + (LONGITUDES + 1) as u32;
            if latitude > 0 {
                triangles.push([a, b, a + 1]);
            }
            if latitude + 1 < LATITUDES {
                triangles.push([a + 1, b, b + 1]);
            }
        }
    }
    (vertices, triangles)
}

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct Snapshot {
    pub model: String,
    pub time_s: f64,
    pub seed: u64,
    pub frame: String,
    pub body: PrimaryBody,
    pub assets: Vec<EntityState>,
    pub events: Vec<Event>,
    pub collisions: u32,
    #[serde(default)]
    pub mission: MissionState,
    #[serde(default)]
    pub observations: Vec<Observation>,
    #[serde(default, with = "decimal_u64")]
    pub rng_state: u64,
    #[serde(default)]
    pub active_contacts: Vec<String>,
}

#[derive(Clone, Debug, Default, Serialize, Deserialize)]
pub struct MissionState {
    pub shuttle_id: String,
    pub target_id: String,
    pub target_distance_m: f64,
    pub relative_speed_mps: f64,
    pub fuel_used_kg: f64,
    pub damage: f64,
    pub status: String,
    #[serde(default)]
    pub score: Option<f64>,
    #[serde(default)]
    pub score_label: String,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct Observation {
    pub id: String,
    pub kind: String,
    pub relative_position_m: [f64; 3],
    pub relative_velocity_mps: [f64; 3],
    pub range_m: f64,
    #[serde(default)]
    pub range_rate_mps: f64,
    #[serde(default)]
    pub diameter_m: f64,
    #[serde(default)]
    pub measurement_time_s: f64,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct PrimaryBody {
    pub name: String,
    pub radii_m: [f64; 3],
    pub mu_m3_s2: f64,
    pub rotation_period_s: f64,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct EntityState {
    pub id: String,
    pub name: String,
    pub kind: String,
    pub position: [f64; 3],
    pub velocity: [f64; 3],
    pub rotation: [f64; 4],
    pub radius_m: f64,
    pub mass_kg: f64,
    pub fuel_kg: f64,
    pub max_thrust_n: f64,
    pub thrust_n: [f64; 3],
    pub status: String,
    #[serde(default)]
    pub torque_nm: [f64; 3],
    #[serde(default)]
    pub angular_velocity_rad_s: [f64; 3],
    #[serde(default)]
    pub commanded_accel_mps2: [f64; 3],
    #[serde(default)]
    pub commanded_angular_accel_rad_s2: [f64; 3],
}

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct Event {
    pub t_s: f64,
    pub asset: String,
    pub message: String,
}

// JSON numbers cannot preserve an arbitrary u64 through JavaScript. Keeping the
// RNG state as decimal text makes room handoffs preserve future sensor noise.
mod decimal_u64 {
    use serde::{Deserialize, Deserializer, Serializer};

    pub fn serialize<S: Serializer>(value: &u64, serializer: S) -> Result<S::Ok, S::Error> {
        serializer.serialize_str(&value.to_string())
    }

    pub fn deserialize<'de, D: Deserializer<'de>>(deserializer: D) -> Result<u64, D::Error> {
        String::deserialize(deserializer)?
            .parse()
            .map_err(serde::de::Error::custom)
    }
}
