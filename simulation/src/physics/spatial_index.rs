//! Actor-sphere birth clearance only. Terrain clearance stays in the physics core.

use crate::domain::EntityId;
use std::{
    collections::{HashMap, hash_map::DefaultHasher},
    hash::BuildHasherDefault,
};

type Cell = [i64; 3];
// Fixed hash initialization keeps index maintenance independent of RNG draws.
type IndexMap<K, V> = HashMap<K, V, BuildHasherDefault<DefaultHasher>>;

#[derive(Clone, Copy)]
struct Sphere {
    id: EntityId,
    position_m: [f64; 3],
    radius_m: f64,
}

#[derive(Clone, Copy)]
struct Location {
    cell: Cell,
    slot: usize,
}

pub(super) struct SpawnIndex {
    cell_size_m: f64,
    maximum_radius_m: f64,
    cells: IndexMap<Cell, Vec<Sphere>>,
    locations: IndexMap<EntityId, Location>,
}

impl SpawnIndex {
    /// The caller supplies the maximum effective actor radius, including the
    /// lander's doubled proxy and the pad's enclosing sphere. No RNG is used.
    pub(super) fn new(maximum_radius_m: f64) -> Self {
        assert!(maximum_radius_m.is_finite() && maximum_radius_m >= 0.0);
        Self {
            cell_size_m: maximum_radius_m + 1.0,
            maximum_radius_m,
            cells: IndexMap::default(),
            locations: IndexMap::default(),
        }
    }

    pub(super) fn insert(&mut self, id: EntityId, position_m: [f64; 3], radius_m: f64) {
        assert!(position_m.into_iter().all(f64::is_finite));
        assert!(radius_m.is_finite() && radius_m >= 0.0);
        self.remove(id);
        // Keep queries conservative even if a later actor exceeds the radius
        // used to choose the fixed cell size. Removal need not shrink this bound.
        self.maximum_radius_m = self.maximum_radius_m.max(radius_m);
        let cell = self.cell(position_m);
        let bucket = self.cells.entry(cell).or_default();
        let slot = bucket.len();
        bucket.push(Sphere {
            id,
            position_m,
            radius_m,
        });
        self.locations.insert(id, Location { cell, slot });
    }

    pub(super) fn remove(&mut self, id: EntityId) {
        let Some(location) = self.locations.remove(&id) else {
            return;
        };
        let bucket = self
            .cells
            .get_mut(&location.cell)
            .expect("indexed actor cell");
        bucket.swap_remove(location.slot);
        if let Some(moved) = bucket.get(location.slot) {
            self.locations
                .get_mut(&moved.id)
                .expect("indexed actor location")
                .slot = location.slot;
        }
        if bucket.is_empty() {
            self.cells.remove(&location.cell);
        }
    }

    pub(super) fn is_clear(
        &self,
        position_m: [f64; 3],
        radius_m: f64,
        clearance_m: f64,
        excluding: Option<EntityId>,
    ) -> bool {
        assert!(position_m.into_iter().all(f64::is_finite));
        assert!(radius_m.is_finite() && radius_m >= 0.0);
        assert!(clearance_m.is_finite() && clearance_m >= 0.0);
        let clear_of = |other: &Sphere| {
            if Some(other.id) == excluding {
                return true;
            }
            let delta: [f64; 3] =
                std::array::from_fn(|axis| position_m[axis] - other.position_m[axis]);
            // Match physics::norm exactly, including its original rounding and
            // strict comparison at contact. Squared distances can change births.
            delta[0].hypot(delta[1]).hypot(delta[2]) > radius_m + other.radius_m + clearance_m
        };
        // Round the AABB outwards. Cell-coordinate casts saturate to i64 at the
        // extremes; actors there share a bucket rather than wrapping or vanishing.
        let reach = (radius_m + self.maximum_radius_m + clearance_m).next_up();
        let lower = self.cell(position_m.map(|value| (value - reach).next_down()));
        let upper = self.cell(position_m.map(|value| (value + reach).next_up()));
        let cell_count = lower
            .into_iter()
            .zip(upper)
            .try_fold(1_u128, |count, (low, high)| {
                count.checked_mul((i128::from(high) - i128::from(low) + 1) as u128)
            });
        if !reach.is_finite()
            || cell_count.is_none_or(|count| count > self.cells.len().max(4_096) as u128)
        {
            // An extreme radius/coordinate span may cover more empty cells than
            // the whole index. This checks every actor, never a capped subset.
            return self.cells.values().flatten().all(clear_of);
        }
        for x in lower[0]..=upper[0] {
            for y in lower[1]..=upper[1] {
                for z in lower[2]..=upper[2] {
                    if let Some(bucket) = self.cells.get(&[x, y, z])
                        && !bucket.iter().all(clear_of)
                    {
                        return false;
                    }
                }
            }
        }
        true
    }

    fn cell(&self, position_m: [f64; 3]) -> Cell {
        position_m.map(|value| (value / self.cell_size_m).floor() as i64)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn brute_clear(
        spheres: &[Sphere],
        position_m: [f64; 3],
        radius_m: f64,
        clearance_m: f64,
        excluding: Option<EntityId>,
    ) -> bool {
        spheres
            .iter()
            .filter(|sphere| Some(sphere.id) != excluding)
            .all(|sphere| {
                super::super::norm(super::super::sub(position_m, sphere.position_m))
                    > radius_m + sphere.radius_m + clearance_m
            })
    }

    #[test]
    fn grid_matches_original_distance_at_contact_and_cell_boundaries() {
        let spheres = [
            Sphere {
                id: EntityId::Lander,
                position_m: [0.0; 3],
                radius_m: 8.0,
            },
            Sphere {
                id: EntityId::LandingPad,
                position_m: [-101.0, 202.0, -303.0],
                radius_m: 35.0,
            },
            Sphere {
                id: EntityId::ReturnShip,
                position_m: [404.0, -505.0, 606.0],
                radius_m: 100.0,
            },
        ];
        let mut index = SpawnIndex::new(100.0);
        for sphere in spheres {
            index.insert(sphere.id, sphere.position_m, sphere.radius_m);
        }
        for sphere in spheres {
            for radius in [0.0, 0.05, 4.0, 100.0] {
                for clearance in [0.0, 1.0, 17.0] {
                    let threshold = radius + sphere.radius_m + clearance;
                    for distance in [threshold.next_down(), threshold, threshold.next_up()] {
                        for axis in 0..3 {
                            for sign in [-1.0, 1.0] {
                                let mut point = sphere.position_m;
                                point[axis] += sign * distance;
                                for excluding in [None, Some(sphere.id)] {
                                    assert_eq!(
                                        index.is_clear(point, radius, clearance, excluding),
                                        brute_clear(&spheres, point, radius, clearance, excluding),
                                        "point={point:?}, radius={radius}, clearance={clearance}, excluding={excluding:?}",
                                    );
                                }
                            }
                        }
                    }
                }
            }
        }
        // A non-axis-aligned exact 3-4-12 triangle exercises the norm, too.
        assert!(!index.is_clear([3.0, 4.0, 12.0], 4.0, 1.0, None));
    }

    #[test]
    fn mixed_radius_grid_matches_brute_force_for_deterministic_probes() {
        let mut seed = 0x849b_a317_e12c_f067_u64;
        let mut sample = || {
            seed ^= seed << 13;
            seed ^= seed >> 7;
            seed ^= seed << 17;
            (seed >> 11) as f64 / 9_007_199_254_740_992.0
        };
        let mut index = SpawnIndex::new(100.0);
        let mut spheres = Vec::new();
        for id in 1..=4_096 {
            let sphere = Sphere {
                id: EntityId::Debris(id),
                position_m: std::array::from_fn(|_| (sample() * 2.0 - 1.0) * 20_000.0),
                radius_m: 0.05 + sample() * 99.95,
            };
            index.insert(sphere.id, sphere.position_m, sphere.radius_m);
            spheres.push(sphere);
        }
        for probe in 0..2_048 {
            let sphere = spheres[probe % spheres.len()];
            let point = std::array::from_fn(|axis| {
                sphere.position_m[axis] + (sample() * 2.0 - 1.0) * 300.0
            });
            let radius = sample() * 100.0;
            let clearance = sample() * 10.0;
            let excluding = (probe % 3 == 0).then_some(sphere.id);
            assert_eq!(
                index.is_clear(point, radius, clearance, excluding),
                brute_clear(&spheres, point, radius, clearance, excluding),
            );
        }
    }

    #[test]
    fn replacement_removal_and_larger_actors_never_leave_a_stale_cell() {
        let mut index = SpawnIndex::new(1.0);
        index.insert(EntityId::Lander, [0.0; 3], 1.0);
        index.insert(EntityId::ReturnShip, [0.5, 0.0, 0.0], 1.0);
        index.insert(EntityId::Lander, [10_000.0, 0.0, 0.0], 5_000.0);
        index.remove(EntityId::ReturnShip);
        assert!(index.is_clear([0.0; 3], 1.0, 1.0, None));
        assert!(!index.is_clear([5_000.0, 0.0, 0.0], 1.0, 1.0, None));
        assert!(index.is_clear([10_000.0, 0.0, 0.0], 1.0, 1.0, Some(EntityId::Lander)));
        index.remove(EntityId::Lander);
        index.remove(EntityId::Lander);
        assert!(index.cells.is_empty());
        assert!(index.locations.is_empty());
    }

    #[test]
    fn extreme_finite_coordinates_saturate_without_dropping_actors() {
        let spheres = [
            Sphere {
                id: EntityId::Lander,
                position_m: [1.0e300, -1.0e300, 0.0],
                radius_m: 4.0,
            },
            Sphere {
                id: EntityId::ReturnShip,
                position_m: [-1.0e300, 1.0e300, 0.0],
                radius_m: 20.0,
            },
        ];
        let mut index = SpawnIndex::new(20.0);
        for sphere in spheres {
            index.insert(sphere.id, sphere.position_m, sphere.radius_m);
        }
        for point in [[0.0; 3], spheres[0].position_m, spheres[1].position_m] {
            for radius in [0.0, 100.0, 1.5e300] {
                assert_eq!(
                    index.is_clear(point, radius, 1.0, None),
                    brute_clear(&spheres, point, radius, 1.0, None),
                );
            }
        }
    }

    #[test]
    fn a_crowded_bucket_checks_every_actor_instead_of_a_neighbor_cap() {
        let mut index = SpawnIndex::new(100.0);
        for id in 1..=5_000 {
            index.insert(EntityId::Debris(id), [50.0; 3], 0.1);
        }
        index.insert(EntityId::Lander, [0.5, 0.0, 0.0], 0.1);
        assert_eq!(index.cells[&[0, 0, 0]].len(), 5_001);
        assert!(!index.is_clear([0.0; 3], 0.1, 1.0, None));
        assert!(index.is_clear([0.0; 3], 0.1, 1.0, Some(EntityId::Lander)));
        index.remove(EntityId::Lander);
        assert!(index.is_clear([0.0; 3], 0.1, 1.0, None));
    }

    #[test]
    fn indexes_all_one_hundred_thousand_distinct_actor_spheres() {
        let mut index = SpawnIndex::new(100.0);
        let position = |id: u32| {
            let offset = id - 1;
            [
                f64::from(offset % 100) * 512.0 - 25_600.0,
                f64::from((offset / 100) % 100) * 512.0 - 25_600.0,
                f64::from(offset / 10_000) * 512.0 - 2_560.0,
            ]
        };
        for id in 1..=100_000 {
            index.insert(
                EntityId::Debris(id),
                position(id),
                0.05 + f64::from(id % 100),
            );
        }
        assert_eq!(index.locations.len(), 100_000);
        assert_eq!(index.cells.values().map(Vec::len).sum::<usize>(), 100_000);
        for id in 1..=100_000 {
            assert!(!index.is_clear(position(id), 1.0, 1.0, None));
            if id % 997 == 0 {
                assert!(index.is_clear(position(id), 1.0, 1.0, Some(EntityId::Debris(id))));
            }
        }
        index.remove(EntityId::Debris(100_000));
        assert!(index.is_clear(position(100_000), 1.0, 1.0, None));
        assert!(!index.is_clear(position(1), 1.0, 1.0, None));
        assert!(index.is_clear([1.0e9; 3], 100.0, 1.0, None));
    }
}
