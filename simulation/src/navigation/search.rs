use std::{cmp::Ordering, collections::HashMap, fmt, sync::Arc};

use bevy::math::DVec3;

use super::geometry::{Capsule, segment_clear};

#[derive(Clone, Copy)]
pub(super) struct SearchSettings {
    pub cell_size_m: f64,
    pub corridor_half_width_m: f64,
    pub max_nodes: usize,
    pub max_expansions: usize,
}

#[derive(Clone, Copy, Debug, serde::Serialize)]
#[serde(rename_all = "snake_case")]
pub(super) enum SearchFailure {
    InvalidInput,
    StartBlocked,
    GoalBlocked,
    NoRouteInCorridor,
    NodeBudgetExceeded,
    ExpansionBudgetExceeded,
}

impl fmt::Display for SearchFailure {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(match self {
            Self::InvalidInput => "Route search received invalid geometry or settings",
            Self::StartBlocked => "The route start is blocked by an inflated obstacle",
            Self::GoalBlocked => "The route goal is blocked by an inflated obstacle",
            Self::NoRouteInCorridor => "No clear route was found within the search corridor",
            Self::NodeBudgetExceeded => "Route search reached its stored-node limit",
            Self::ExpansionBudgetExceeded => "Route search reached its expansion limit",
        })
    }
}

pub(super) enum SearchProgress {
    Searching,
    Found(Arc<Vec<DVec3>>),
    Failed(SearchFailure),
}

/// A bounded geometric advisory, not a globally complete or flight-feasible plan.
pub(super) struct Search {
    start: DVec3,
    goal: DVec3,
    settings: SearchSettings,
    frame: Frame,
    collisions: CollisionIndex,
    nodes: Vec<Node>,
    by_key: HashMap<Key, usize>,
    // An indexed heap has one entry per open node, including after relaxation.
    // A duplicate-entry BinaryHeap would not respect the storage limit.
    open: Vec<usize>,
    reverse_path: Vec<DVec3>,
    path: Arc<Vec<DVec3>>,
    expanded: usize,
    phase: Phase,
}

impl Search {
    /// Builds the obstacle index and, only for a detour, reserves O(max_nodes)
    /// storage here, outside advance's work budget. No whole-corridor grid is
    /// allocated. Each path buffer allows one extra point for the exact goal.
    pub(super) fn new(
        start: DVec3,
        goal: DVec3,
        obstacles: Vec<Capsule>,
        settings: SearchSettings,
    ) -> Result<Self, SearchFailure> {
        let frame = Frame::new(start, goal, settings)?;
        let collisions = CollisionIndex::new(obstacles, start, goal)?;
        if !collisions.clear(start, start) {
            return Err(SearchFailure::StartBlocked);
        }
        if !collisions.clear(goal, goal) {
            return Err(SearchFailure::GoalBlocked);
        }

        let direct = collisions.clear(start, goal);
        let mut nodes = Vec::new();
        let mut by_key = HashMap::new();
        let mut open = Vec::new();
        let mut reverse_path = Vec::new();
        let path = if direct {
            if start == goal {
                vec![start]
            } else {
                vec![start, goal]
            }
        } else {
            let path_capacity = settings
                .max_nodes
                .checked_add(1)
                .ok_or(SearchFailure::InvalidInput)?;
            nodes = reserved_vec(settings.max_nodes)?;
            open = reserved_vec(settings.max_nodes)?;
            by_key
                .try_reserve(settings.max_nodes)
                .map_err(|_| SearchFailure::InvalidInput)?;
            reverse_path = reserved_vec(path_capacity)?;
            let mut path = reserved_vec(path_capacity)?;
            path.push(start);
            path
        };
        let mut search = Self {
            start,
            goal,
            settings,
            frame,
            collisions,
            nodes,
            by_key,
            open,
            reverse_path,
            path: Arc::new(path),
            expanded: 0,
            phase: if direct {
                Phase::Found
            } else {
                Phase::Searching
            },
        };
        if !direct {
            let heuristic = distance(start, goal).ok_or(SearchFailure::InvalidInput)?;
            search.nodes.push(Node {
                key: Key::ORIGIN,
                point: start,
                cost: 0.0,
                heuristic,
                parent: None,
                open_slot: None,
            });
            search.by_key.insert(Key::ORIGIN, 0);
            search.push_open(0);
        }
        Ok(search)
    }

    /// Each unit expands at most one 26-neighbor node, or processes one path
    /// point. Buffers never grow and completed routes only clone an Arc here.
    /// Collision queries can still visit every capsule. This bounds node work,
    /// not wall-clock time independently of the obstacle count.
    pub(super) fn advance(&mut self, node_budget: usize) -> SearchProgress {
        for _ in 0..node_budget {
            if matches!(self.phase, Phase::Found | Phase::Failed(_)) {
                break;
            }
            let phase =
                std::mem::replace(&mut self.phase, Phase::Failed(SearchFailure::InvalidInput));
            self.phase = match phase {
                Phase::Searching => self.expand_one(),
                Phase::Tracing(trace) => self.trace_one(trace),
                Phase::Smoothing(smoothing) => self.smooth_one(smoothing),
                terminal => terminal,
            };
        }
        match &self.phase {
            Phase::Found => SearchProgress::Found(Arc::clone(&self.path)),
            Phase::Failed(failure) => SearchProgress::Failed(*failure),
            Phase::Searching | Phase::Tracing(_) | Phase::Smoothing(_) => SearchProgress::Searching,
        }
    }

    pub(super) fn expanded(&self) -> usize {
        self.expanded
    }

    fn expand_one(&mut self) -> Phase {
        if self.open.is_empty() {
            return Phase::Failed(SearchFailure::NoRouteInCorridor);
        }
        if self.expanded >= self.settings.max_expansions {
            return Phase::Failed(SearchFailure::ExpansionBudgetExceeded);
        }
        let current = self.pop_open();
        self.expanded += 1;
        let point = self.nodes[current].point;

        // The goal is never rounded onto the lattice. This connector stays in
        // the convex corridor and is checked against the same frozen capsules.
        if self.collisions.clear(point, self.goal) {
            self.reverse_path.push(self.goal);
            return Phase::Tracing(Trace {
                cursor: current,
                remaining: self.by_key.len(),
            });
        }

        let key = self.nodes[current].key;
        let cost = self.nodes[current].cost;
        for along in -1..=1 {
            for across in -1..=1 {
                for up in -1..=1 {
                    if along == 0 && across == 0 && up == 0 {
                        continue;
                    }
                    let Some(neighbor) = key.offset(along, across, up) else {
                        return Phase::Failed(SearchFailure::InvalidInput);
                    };
                    if !self.frame.contains(neighbor) {
                        continue;
                    }
                    let known = self.by_key.get(&neighbor).copied();
                    let candidate = match known {
                        Some(index) => self.nodes[index].point,
                        None => match self.frame.point(neighbor) {
                            Some(point) => point,
                            None => return Phase::Failed(SearchFailure::InvalidInput),
                        },
                    };
                    let Some(length) = distance(point, candidate) else {
                        return Phase::Failed(SearchFailure::InvalidInput);
                    };
                    let tentative = cost + length;
                    if !tentative.is_finite() || length <= 0.0 || tentative <= cost {
                        return Phase::Failed(SearchFailure::InvalidInput);
                    }
                    if known.is_some_and(|index| tentative >= self.nodes[index].cost) {
                        continue;
                    }
                    // Checking the complete edge forbids diagonal corner cuts.
                    if !self.collisions.clear(point, candidate) {
                        continue;
                    }
                    let index = if let Some(index) = known {
                        self.nodes[index].cost = tentative;
                        self.nodes[index].parent = Some(current);
                        index
                    } else {
                        if self.nodes.len() >= self.settings.max_nodes {
                            return Phase::Failed(SearchFailure::NodeBudgetExceeded);
                        }
                        let Some(heuristic) = distance(candidate, self.goal) else {
                            return Phase::Failed(SearchFailure::InvalidInput);
                        };
                        if !(tentative + heuristic).is_finite() {
                            return Phase::Failed(SearchFailure::InvalidInput);
                        }
                        let index = self.nodes.len();
                        self.nodes.push(Node {
                            key: neighbor,
                            point: candidate,
                            cost: tentative,
                            heuristic,
                            parent: Some(current),
                            open_slot: None,
                        });
                        self.by_key.insert(neighbor, index);
                        index
                    };
                    if let Some(slot) = self.nodes[index].open_slot {
                        self.sift_up(slot);
                    } else {
                        self.push_open(index);
                    }
                }
            }
        }

        if self.open.is_empty() {
            Phase::Failed(SearchFailure::NoRouteInCorridor)
        } else if self.expanded >= self.settings.max_expansions {
            Phase::Failed(SearchFailure::ExpansionBudgetExceeded)
        } else {
            Phase::Searching
        }
    }

    fn trace_one(&mut self, mut trace: Trace) -> Phase {
        if trace.remaining == 0 {
            return Phase::Failed(SearchFailure::InvalidInput);
        }
        trace.remaining -= 1;
        let Some(node) = self.nodes.get(trace.cursor) else {
            return Phase::Failed(SearchFailure::InvalidInput);
        };
        let Some(&previous) = self.reverse_path.last() else {
            return Phase::Failed(SearchFailure::InvalidInput);
        };
        if !self.collisions.clear(node.point, previous) {
            return Phase::Failed(SearchFailure::InvalidInput);
        }
        if node.point != previous {
            if self.reverse_path.len() > self.settings.max_nodes {
                return Phase::Failed(SearchFailure::InvalidInput);
            }
            self.reverse_path.push(node.point);
        }
        if let Some(parent) = node.parent {
            trace.cursor = parent;
            Phase::Tracing(trace)
        } else if trace.cursor == 0 && node.point == self.start {
            self.reverse_path.pop();
            Phase::Smoothing(Smoothing {
                previous: self.start,
            })
        } else {
            Phase::Failed(SearchFailure::InvalidInput)
        }
    }

    fn smooth_one(&mut self, mut smoothing: Smoothing) -> Phase {
        let Some(candidate) = self.reverse_path.pop() else {
            return Phase::Failed(SearchFailure::InvalidInput);
        };
        let Some(&anchor) = self.path.last() else {
            return Phase::Failed(SearchFailure::InvalidInput);
        };
        // Linear greedy visibility pulling. A failed longer shortcut keeps the
        // last checked point; no unchecked shortcut enters the returned path.
        let keep_previous = !self.collisions.clear(anchor, candidate);
        if keep_previous
            && (smoothing.previous == anchor
                || !self.collisions.clear(smoothing.previous, candidate))
        {
            return Phase::Failed(SearchFailure::InvalidInput);
        }
        let Some(path) = Arc::get_mut(&mut self.path) else {
            return Phase::Failed(SearchFailure::InvalidInput);
        };
        if keep_previous {
            if path.len() > self.settings.max_nodes {
                return Phase::Failed(SearchFailure::InvalidInput);
            }
            path.push(smoothing.previous);
        }
        smoothing.previous = candidate;
        if self.reverse_path.is_empty() {
            if candidate != self.goal {
                return Phase::Failed(SearchFailure::InvalidInput);
            }
            if path.last().copied() != Some(candidate) {
                if path.len() > self.settings.max_nodes {
                    return Phase::Failed(SearchFailure::InvalidInput);
                }
                path.push(candidate);
            }
            Phase::Found
        } else {
            Phase::Smoothing(smoothing)
        }
    }

    fn preferred(&self, left: usize, right: usize) -> bool {
        let left = &self.nodes[left];
        let right = &self.nodes[right];
        (left.cost + left.heuristic)
            .total_cmp(&(right.cost + right.heuristic))
            .then_with(|| left.heuristic.total_cmp(&right.heuristic))
            .then_with(|| left.key.cmp(&right.key))
            == Ordering::Less
    }

    fn push_open(&mut self, index: usize) {
        let slot = self.open.len();
        self.open.push(index);
        self.nodes[index].open_slot = Some(slot);
        self.sift_up(slot);
    }

    fn pop_open(&mut self) -> usize {
        let first = self.open[0];
        let last = self.open.pop().expect("the open heap is nonempty");
        self.nodes[first].open_slot = None;
        if !self.open.is_empty() {
            self.open[0] = last;
            self.nodes[last].open_slot = Some(0);
            self.sift_down(0);
        }
        first
    }

    fn swap_open(&mut self, left: usize, right: usize) {
        self.open.swap(left, right);
        self.nodes[self.open[left]].open_slot = Some(left);
        self.nodes[self.open[right]].open_slot = Some(right);
    }

    fn sift_up(&mut self, mut slot: usize) {
        while slot > 0 {
            let parent = (slot - 1) / 2;
            if !self.preferred(self.open[slot], self.open[parent]) {
                break;
            }
            self.swap_open(slot, parent);
            slot = parent;
        }
    }

    fn sift_down(&mut self, mut slot: usize) {
        // Written this way so even the index arithmetic cannot wrap.
        while slot < self.open.len() / 2 {
            let left = slot * 2 + 1;
            let right = left + 1;
            let child =
                if right < self.open.len() && self.preferred(self.open[right], self.open[left]) {
                    right
                } else {
                    left
                };
            if !self.preferred(self.open[child], self.open[slot]) {
                break;
            }
            self.swap_open(slot, child);
            slot = child;
        }
    }
}

fn reserved_vec<T>(capacity: usize) -> Result<Vec<T>, SearchFailure> {
    let mut items = Vec::new();
    items
        .try_reserve_exact(capacity)
        .map_err(|_| SearchFailure::InvalidInput)?;
    Ok(items)
}

enum Phase {
    Searching,
    Tracing(Trace),
    Smoothing(Smoothing),
    Found,
    Failed(SearchFailure),
}

struct Trace {
    cursor: usize,
    remaining: usize,
}

struct Smoothing {
    previous: DVec3,
}

struct Node {
    key: Key,
    point: DVec3,
    cost: f64,
    heuristic: f64,
    parent: Option<usize>,
    open_slot: Option<usize>,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash, PartialOrd, Ord)]
struct Key {
    along: i32,
    across: i32,
    up: i32,
}

impl Key {
    const ORIGIN: Self = Self {
        along: 0,
        across: 0,
        up: 0,
    };

    fn offset(self, along: i32, across: i32, up: i32) -> Option<Self> {
        Some(Self {
            along: self.along.checked_add(along)?,
            across: self.across.checked_add(across)?,
            up: self.up.checked_add(up)?,
        })
    }
}

struct Frame {
    start: DVec3,
    forward: DVec3,
    across: DVec3,
    up: DVec3,
    cell_size_m: f64,
    along_max: i32,
    lateral_max: i32,
}

impl Frame {
    fn new(start: DVec3, goal: DVec3, settings: SearchSettings) -> Result<Self, SearchFailure> {
        if !start.is_finite()
            || !goal.is_finite()
            || !settings.cell_size_m.is_finite()
            || settings.cell_size_m <= 0.0
            || !settings.corridor_half_width_m.is_finite()
            || settings.corridor_half_width_m <= 0.0
            || settings.max_nodes == 0
            || settings.max_expansions == 0
        {
            return Err(SearchFailure::InvalidInput);
        }
        let length = distance(start, goal).ok_or(SearchFailure::InvalidInput)?;
        let along_cells = (length / settings.cell_size_m).floor();
        let lateral_cells = (settings.corridor_half_width_m / settings.cell_size_m).floor();
        if !along_cells.is_finite()
            || along_cells > f64::from(i32::MAX - 3)
            || !lateral_cells.is_finite()
            || lateral_cells > f64::from(i32::MAX - 1)
        {
            return Err(SearchFailure::InvalidInput);
        }

        let forward = if length == 0.0 {
            DVec3::X
        } else {
            (goal - start) / length
        };
        let absolute = forward.abs();
        let reference = if absolute.x <= absolute.y && absolute.x <= absolute.z {
            DVec3::X
        } else if absolute.y <= absolute.z {
            DVec3::Y
        } else {
            DVec3::Z
        };
        let across = forward.cross(reference);
        let across_length = norm(across).ok_or(SearchFailure::InvalidInput)?;
        if across_length == 0.0 {
            return Err(SearchFailure::InvalidInput);
        }
        let across = across / across_length;
        let up = forward.cross(across);
        let frame = Self {
            start,
            forward,
            across,
            up,
            cell_size_m: settings.cell_size_m,
            // Two cells behind departure, and at most two beyond the exact goal.
            along_max: along_cells as i32 + 2,
            lateral_max: lateral_cells as i32,
        };
        for along in [-2, frame.along_max] {
            for across in [-frame.lateral_max, frame.lateral_max] {
                for up in [-frame.lateral_max, frame.lateral_max] {
                    let point = frame
                        .point(Key { along, across, up })
                        .ok_or(SearchFailure::InvalidInput)?;
                    distance(point, start).ok_or(SearchFailure::InvalidInput)?;
                    distance(point, goal).ok_or(SearchFailure::InvalidInput)?;
                }
            }
        }
        Ok(frame)
    }

    fn contains(&self, key: Key) -> bool {
        (-2..=self.along_max).contains(&key.along)
            && (-self.lateral_max..=self.lateral_max).contains(&key.across)
            && (-self.lateral_max..=self.lateral_max).contains(&key.up)
    }

    fn point(&self, key: Key) -> Option<DVec3> {
        if key == Key::ORIGIN {
            return Some(self.start);
        }
        let point = self.start
            + self.forward * (f64::from(key.along) * self.cell_size_m)
            + self.across * (f64::from(key.across) * self.cell_size_m)
            + self.up * (f64::from(key.up) * self.cell_size_m);
        point.is_finite().then_some(point)
    }
}

fn distance(from: DVec3, to: DVec3) -> Option<f64> {
    norm(to - from)
}

fn norm(value: DVec3) -> Option<f64> {
    if !value.is_finite() {
        return None;
    }
    let length = value.length();
    if length.is_finite() && (length > 0.0 || value == DVec3::ZERO) {
        return Some(length);
    }
    let length = value.x.hypot(value.y).hypot(value.z);
    length.is_finite().then_some(length)
}

#[derive(Clone, Copy)]
struct Aabb {
    min: DVec3,
    max: DVec3,
}

impl Aabb {
    fn padded(from: DVec3, to: DVec3, radius: f64) -> Option<Self> {
        if !from.is_finite() || !to.is_finite() || !radius.is_finite() || radius < 0.0 {
            return None;
        }
        // This padding covers geometry's 256-EPSILON projection allowance.
        // Padding both boxes also covers subtraction between distant endpoints.
        let magnitude = from
            .abs()
            .max_element()
            .max(to.abs().max_element())
            .max(radius);
        let padding = radius + magnitude * (1024.0 * f64::EPSILON);
        let min = from.min(to) - DVec3::splat(padding);
        let max = from.max(to) + DVec3::splat(padding);
        (min.is_finite() && max.is_finite()).then_some(Self { min, max })
    }

    fn union(self, other: Self) -> Self {
        Self {
            min: self.min.min(other.min),
            max: self.max.max(other.max),
        }
    }

    fn overlaps(self, other: Self) -> bool {
        self.min.cmple(other.max).all() && other.min.cmple(self.max).all()
    }

    fn center(self, axis: usize) -> f64 {
        self.min[axis] * 0.5 + self.max[axis] * 0.5
    }
}

enum BvhNode {
    Leaf {
        bounds: Aabb,
        obstacle: usize,
    },
    Branch {
        bounds: Aabb,
        left: usize,
        right: usize,
    },
}

struct CollisionIndex {
    obstacles: Vec<Capsule>,
    nodes: Vec<BvhNode>,
    root: Option<usize>,
}

impl CollisionIndex {
    fn new(obstacles: Vec<Capsule>, start: DVec3, goal: DVec3) -> Result<Self, SearchFailure> {
        let mut bounds = Vec::new();
        for obstacle in &obstacles {
            if !obstacle.from.is_finite()
                || !obstacle.to.is_finite()
                || !obstacle.radius_m.is_finite()
                || obstacle.radius_m <= 0.0
                || !(obstacle.to - obstacle.from).is_finite()
                || !(start - obstacle.from).is_finite()
                || !(start - obstacle.to).is_finite()
                || !(goal - obstacle.from).is_finite()
                || !(goal - obstacle.to).is_finite()
            {
                return Err(SearchFailure::InvalidInput);
            }
            bounds.push(
                Aabb::padded(obstacle.from, obstacle.to, obstacle.radius_m)
                    .ok_or(SearchFailure::InvalidInput)?,
            );
        }
        let mut order: Vec<_> = (0..obstacles.len()).collect();
        let mut nodes = Vec::new();
        let root = if order.is_empty() {
            None
        } else {
            Some(Self::build(&mut order, &bounds, &mut nodes))
        };
        Ok(Self {
            obstacles,
            nodes,
            root,
        })
    }

    fn build(order: &mut [usize], boxes: &[Aabb], nodes: &mut Vec<BvhNode>) -> usize {
        let bounds = order
            .iter()
            .skip(1)
            .fold(boxes[order[0]], |bounds, &index| bounds.union(boxes[index]));
        let node = if order.len() == 1 {
            BvhNode::Leaf {
                bounds,
                obstacle: order[0],
            }
        } else {
            let extent = bounds.max * 0.5 - bounds.min * 0.5;
            let axis = if extent.x >= extent.y && extent.x >= extent.z {
                0
            } else if extent.y >= extent.z {
                1
            } else {
                2
            };
            order.sort_unstable_by(|&left, &right| {
                boxes[left]
                    .center(axis)
                    .total_cmp(&boxes[right].center(axis))
                    .then_with(|| left.cmp(&right))
            });
            let middle = order.len() / 2;
            let (left, right) = order.split_at_mut(middle);
            BvhNode::Branch {
                bounds,
                left: Self::build(left, boxes, nodes),
                right: Self::build(right, boxes, nodes),
            }
        };
        let index = nodes.len();
        nodes.push(node);
        index
    }

    fn clear(&self, from: DVec3, to: DVec3) -> bool {
        if !(to - from).is_finite() {
            return false;
        }
        let Some(bounds) = Aabb::padded(from, to, 0.0) else {
            return false;
        };
        self.root
            .is_none_or(|root| self.clear_node(root, bounds, from, to))
    }

    fn clear_node(&self, index: usize, query: Aabb, from: DVec3, to: DVec3) -> bool {
        match self.nodes[index] {
            BvhNode::Leaf { bounds, obstacle } => {
                !bounds.overlaps(query)
                    || segment_clear(from, to, std::slice::from_ref(&self.obstacles[obstacle]))
            }
            BvhNode::Branch {
                bounds,
                left,
                right,
            } => {
                !bounds.overlaps(query)
                    || (self.clear_node(left, query, from, to)
                        && self.clear_node(right, query, from, to))
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn settings() -> SearchSettings {
        SearchSettings {
            cell_size_m: 1.0,
            corridor_half_width_m: 4.0,
            max_nodes: 4_000,
            max_expansions: 2_000,
        }
    }

    fn sphere(center: DVec3, radius_m: f64) -> Capsule {
        Capsule {
            from: center,
            to: center,
            radius_m,
        }
    }

    fn finish(search: &mut Search, budget: usize) -> Result<Arc<Vec<DVec3>>, SearchFailure> {
        let capacities = (
            search.nodes.capacity(),
            search.open.capacity(),
            search.by_key.capacity(),
            search.reverse_path.capacity(),
            search.path.capacity(),
        );
        let buffers = (
            search.nodes.as_ptr(),
            search.open.as_ptr(),
            search.reverse_path.as_ptr(),
            search.path.as_ptr(),
        );
        for _ in 0..20_000 {
            let before = search.expanded();
            let progress = search.advance(budget);
            assert!(search.expanded() - before <= budget);
            assert!(search.nodes.len() <= search.settings.max_nodes);
            assert_eq!(search.by_key.len(), search.nodes.len());
            assert!(search.open.len() <= search.nodes.len());
            assert!(search.reverse_path.len().saturating_sub(1) <= search.settings.max_nodes);
            assert!(search.path.len().saturating_sub(1) <= search.settings.max_nodes);
            assert_eq!(
                (
                    search.nodes.capacity(),
                    search.open.capacity(),
                    search.by_key.capacity(),
                    search.reverse_path.capacity(),
                    search.path.capacity(),
                ),
                capacities,
            );
            assert_eq!(
                (
                    search.nodes.as_ptr(),
                    search.open.as_ptr(),
                    search.reverse_path.as_ptr(),
                    search.path.as_ptr(),
                ),
                buffers,
            );
            match progress {
                SearchProgress::Found(path) => return Ok(path),
                SearchProgress::Failed(failure) => return Err(failure),
                SearchProgress::Searching => {}
            }
        }
        panic!("bounded search did not terminate");
    }

    fn assert_clear_path(path: &[DVec3], start: DVec3, goal: DVec3, obstacles: &[Capsule]) {
        assert_eq!(path.first().copied(), Some(start));
        assert_eq!(path.last().copied(), Some(goal));
        assert!(
            path.windows(2)
                .all(|edge| segment_clear(edge[0], edge[1], obstacles))
        );
    }

    #[test]
    fn empty_space_returns_exact_endpoints_without_expansion() {
        let start = DVec3::new(1.25, -2.5, 0.125);
        let goal = DVec3::new(7.3, 4.1, -1.9);
        let mut search = Search::new(start, goal, vec![], settings()).unwrap();
        assert_eq!(search.nodes.capacity(), 0);
        assert_eq!(search.open.capacity(), 0);
        assert_eq!(search.by_key.capacity(), 0);
        assert_eq!(search.reverse_path.capacity(), 0);
        assert!(search.path.capacity() <= 2);
        assert_eq!(finish(&mut search, 1).unwrap().as_slice(), &[start, goal]);
        assert_eq!(search.expanded(), 0);
        let mut same = Search::new(start, start, vec![], settings()).unwrap();
        assert_eq!(finish(&mut same, 1).unwrap().as_slice(), &[start]);
    }

    #[test]
    fn sphere_detour_is_clear_and_exact_goal_is_not_rounded() {
        let start = DVec3::ZERO;
        let goal = DVec3::new(8.35, 0.0, 0.0);
        let obstacles = vec![sphere(DVec3::X * 4.0, 1.3)];
        let mut search = Search::new(start, goal, obstacles.clone(), settings()).unwrap();
        assert!(matches!(search.advance(0), SearchProgress::Searching));
        assert_eq!(search.expanded(), 0);
        let path = finish(&mut search, 1).unwrap();
        assert!(path.len() > 2);
        assert_clear_path(&path, start, goal, &obstacles);
        let length: f64 = path
            .windows(2)
            .map(|edge| (edge[1] - edge[0]).length())
            .sum();
        assert!(length > (goal - start).length());
    }

    #[test]
    fn wall_requires_a_three_dimensional_detour() {
        let start = DVec3::ZERO;
        let goal = DVec3::X * 8.0;
        let obstacles = vec![Capsule {
            from: DVec3::new(4.0, -20.0, 0.0),
            to: DVec3::new(4.0, 20.0, 0.0),
            radius_m: 1.25,
        }];
        let mut search = Search::new(start, goal, obstacles.clone(), settings()).unwrap();
        let path = finish(&mut search, 7).unwrap();
        assert!(path.iter().any(|point| point.z.abs() > 1.25));
        assert_clear_path(&path, start, goal, &obstacles);
    }

    #[test]
    fn corridor_follows_departure_to_goal_not_world_x() {
        let start = DVec3::new(20.0, -10.0, 3.0);
        let direction = DVec3::new(1.0, 2.0, 3.0).normalize();
        let goal = start + direction * 8.35;
        let obstacles = vec![sphere(start + direction * 4.0, 1.3)];
        let mut search = Search::new(start, goal, obstacles.clone(), settings()).unwrap();
        let path = finish(&mut search, 3).unwrap();
        for point in path.iter() {
            let local = *point - start;
            assert!(local.dot(search.frame.forward) >= -2.000_001);
            assert!(local.dot(search.frame.forward) <= 10.000_001);
            assert!(local.dot(search.frame.across).abs() <= 4.000_001);
            assert!(local.dot(search.frame.up).abs() <= 4.000_001);
        }
        assert_clear_path(&path, start, goal, &obstacles);
    }

    #[test]
    fn endpoint_blockage_uses_exact_points() {
        let goal = DVec3::new(8.35, 0.0, 0.0);
        assert!(matches!(
            Search::new(
                DVec3::ZERO,
                goal,
                vec![sphere(DVec3::ZERO, 0.1)],
                settings()
            ),
            Err(SearchFailure::StartBlocked)
        ));
        assert!(matches!(
            Search::new(DVec3::ZERO, goal, vec![sphere(goal, 0.1)], settings()),
            Err(SearchFailure::GoalBlocked)
        ));
        // The nearest lattice point is blocked, but the exact goal is clear.
        let obstacles = vec![sphere(DVec3::X * 8.0, 0.2)];
        let mut search = Search::new(DVec3::ZERO, goal, obstacles.clone(), settings()).unwrap();
        let path = finish(&mut search, 5).unwrap();
        assert_clear_path(&path, DVec3::ZERO, goal, &obstacles);
    }

    #[test]
    fn sealed_corridor_and_budgets_terminate() {
        let start = DVec3::ZERO;
        let goal = DVec3::X * 8.0;
        let wall = vec![Capsule {
            from: DVec3::new(4.0, -100.0, 0.0),
            to: DVec3::new(4.0, 100.0, 0.0),
            radius_m: 1.25,
        }];
        let narrow = SearchSettings {
            corridor_half_width_m: 1.0,
            ..settings()
        };
        let mut sealed = Search::new(start, goal, wall, narrow).unwrap();
        assert!(matches!(
            finish(&mut sealed, 11),
            Err(SearchFailure::NoRouteInCorridor)
        ));

        let obstacles = vec![sphere(DVec3::X * 4.0, 1.3)];
        let mut node_limited = Search::new(
            start,
            goal,
            obstacles.clone(),
            SearchSettings {
                max_nodes: 1,
                ..settings()
            },
        )
        .unwrap();
        assert!(matches!(
            finish(&mut node_limited, 1),
            Err(SearchFailure::NodeBudgetExceeded)
        ));
        assert_eq!(node_limited.nodes.len(), 1);
        let mut expansion_limited = Search::new(
            start,
            goal,
            obstacles,
            SearchSettings {
                max_expansions: 1,
                ..settings()
            },
        )
        .unwrap();
        assert!(matches!(
            finish(&mut expansion_limited, 1),
            Err(SearchFailure::ExpansionBudgetExceeded)
        ));
        assert_eq!(expansion_limited.expanded(), 1);
        assert!(matches!(
            expansion_limited.advance(0),
            SearchProgress::Failed(SearchFailure::ExpansionBudgetExceeded)
        ));
    }

    #[test]
    fn deterministic_across_increment_sizes_and_repeated_searches() {
        let start = DVec3::ZERO;
        let goal = DVec3::X * 10.35;
        let obstacles = vec![
            sphere(DVec3::X * 4.0, 1.3),
            sphere(DVec3::new(7.0, 1.0, 0.0), 1.1),
        ];
        let mut one = Search::new(start, goal, obstacles.clone(), settings()).unwrap();
        let expected = finish(&mut one, 1).unwrap();
        for budget in [2, 17, 256] {
            let mut other = Search::new(start, goal, obstacles.clone(), settings()).unwrap();
            assert_eq!(finish(&mut other, budget).unwrap(), expected);
            assert_eq!(other.expanded(), one.expanded());
        }
        assert_clear_path(&expected, start, goal, &obstacles);
    }

    #[test]
    fn repeated_completed_progress_shares_the_same_route_even_with_zero_budget() {
        let start = DVec3::ZERO;
        let goal = DVec3::X * 8.35;
        let obstacles = vec![sphere(DVec3::X * 4.0, 1.3)];
        let mut search = Search::new(start, goal, obstacles.clone(), settings()).unwrap();
        let route = finish(&mut search, 1).unwrap();
        let expanded = search.expanded();
        assert!(route.len() > 2);
        for budget in [0, 1, 256, usize::MAX] {
            let SearchProgress::Found(again) = search.advance(budget) else {
                panic!("a completed route must remain completed");
            };
            assert!(Arc::ptr_eq(&route, &again));
            assert_eq!(route.as_ptr(), again.as_ptr());
            assert_eq!(search.expanded(), expanded);
            assert_clear_path(&again, start, goal, &obstacles);
        }
    }

    #[test]
    fn sparse_storage_uses_the_node_cap_not_corridor_volume() {
        let config = SearchSettings {
            cell_size_m: 40.0,
            corridor_half_width_m: 40_000_000.0,
            max_nodes: 4,
            max_expansions: 10,
        };
        let mut search = Search::new(
            DVec3::ZERO,
            DVec3::X * 3_500.0,
            vec![sphere(DVec3::X * 1_750.0, 100.0)],
            config,
        )
        .unwrap();
        assert_eq!(search.nodes.len(), 1);
        assert_eq!(search.open.len(), 1);
        assert_eq!(search.nodes.capacity(), config.max_nodes);
        assert_eq!(search.open.capacity(), config.max_nodes);
        assert!(search.by_key.capacity() >= config.max_nodes);
        assert_eq!(search.reverse_path.capacity(), config.max_nodes + 1);
        assert_eq!(search.path.capacity(), config.max_nodes + 1);
        assert!(matches!(
            finish(&mut search, 1),
            Err(SearchFailure::NodeBudgetExceeded)
        ));
        assert_eq!(search.nodes.len(), config.max_nodes);
        assert!(search.nodes.capacity() <= config.max_nodes);
        assert!(search.open.capacity() <= config.max_nodes);
    }

    #[test]
    fn unrepresentable_detour_capacity_is_rejected_without_reserving_it() {
        let config = SearchSettings {
            max_nodes: usize::MAX,
            ..settings()
        };
        assert!(matches!(
            Search::new(
                DVec3::ZERO,
                DVec3::X * 8.0,
                vec![sphere(DVec3::X * 4.0, 1.3)],
                config,
            ),
            Err(SearchFailure::InvalidInput)
        ));
        let direct = Search::new(DVec3::ZERO, DVec3::X * 8.0, vec![], config).unwrap();
        assert_eq!(direct.nodes.capacity(), 0);
        assert_eq!(direct.by_key.capacity(), 0);
        assert!(direct.path.capacity() <= 2);
    }

    #[test]
    fn invalid_settings_geometry_and_index_overflow_are_rejected() {
        for invalid in [0.0, -1.0, f64::NAN, f64::INFINITY] {
            for config in [
                SearchSettings {
                    cell_size_m: invalid,
                    ..settings()
                },
                SearchSettings {
                    corridor_half_width_m: invalid,
                    ..settings()
                },
            ] {
                assert!(matches!(
                    Search::new(DVec3::ZERO, DVec3::X, vec![], config),
                    Err(SearchFailure::InvalidInput)
                ));
            }
            assert!(matches!(
                Search::new(
                    DVec3::ZERO,
                    DVec3::X,
                    vec![sphere(DVec3::Y * 10.0, invalid)],
                    settings()
                ),
                Err(SearchFailure::InvalidInput)
            ));
        }
        for config in [
            SearchSettings {
                max_nodes: 0,
                ..settings()
            },
            SearchSettings {
                max_expansions: 0,
                ..settings()
            },
            SearchSettings {
                cell_size_m: 1e-300,
                ..settings()
            },
            SearchSettings {
                corridor_half_width_m: f64::MAX,
                ..settings()
            },
        ] {
            assert!(matches!(
                Search::new(DVec3::ZERO, DVec3::X, vec![], config),
                Err(SearchFailure::InvalidInput)
            ));
        }
        assert!(matches!(
            Search::new(DVec3::splat(f64::NAN), DVec3::X, vec![], settings()),
            Err(SearchFailure::InvalidInput)
        ));
        assert!(matches!(
            Search::new(DVec3::ZERO, DVec3::splat(f64::INFINITY), vec![], settings()),
            Err(SearchFailure::InvalidInput)
        ));
        assert!(matches!(
            Search::new(
                DVec3::ZERO,
                DVec3::X,
                vec![sphere(DVec3::splat(f64::NAN), 1.0)],
                settings()
            ),
            Err(SearchFailure::InvalidInput)
        ));
        assert!(matches!(
            Search::new(
                DVec3::X * -f64::MAX,
                DVec3::X * f64::MAX,
                vec![],
                settings()
            ),
            Err(SearchFailure::InvalidInput)
        ));
    }

    #[test]
    fn broadphase_keeps_large_capsules_with_endpoints_outside_the_corridor() {
        let obstacles = vec![
            Capsule {
                from: DVec3::new(4.0, -10_000.0, 0.0),
                to: DVec3::new(4.0, 10_000.0, 0.0),
                radius_m: 1.25,
            },
            sphere(DVec3::new(4.0, 0.0, 1_000.0), 999.0),
            sphere(DVec3::splat(-1_000.0), 1.0),
        ];
        let start = DVec3::ZERO;
        let goal = DVec3::X * 8.0;
        let index = CollisionIndex::new(obstacles.clone(), start, goal).unwrap();
        assert!(!index.clear(start, goal));
        for y in -4..=4 {
            for z in -4..=4 {
                let from = DVec3::new(0.0, f64::from(y), f64::from(z));
                let to = DVec3::new(8.0, f64::from(-z), f64::from(y));
                assert_eq!(index.clear(from, to), segment_clear(from, to, &obstacles));
            }
        }
        let mut search = Search::new(start, goal, obstacles.clone(), settings()).unwrap();
        let path = finish(&mut search, 13).unwrap();
        assert_clear_path(&path, start, goal, &obstacles);
    }

    #[test]
    fn tracing_a_corrupt_parent_cycle_is_bounded() {
        let mut search = Search::new(
            DVec3::ZERO,
            DVec3::X * 8.0,
            vec![sphere(DVec3::X * 4.0, 1.3)],
            settings(),
        )
        .unwrap();
        search.nodes[0].parent = Some(0);
        search.reverse_path.push(search.start);
        search.phase = Phase::Tracing(Trace {
            cursor: 0,
            remaining: search.by_key.len(),
        });
        assert!(matches!(
            finish(&mut search, 1),
            Err(SearchFailure::InvalidInput)
        ));
    }
}
