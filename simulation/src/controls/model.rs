#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub(crate) enum MovementTarget {
    Ship,
    DebugCamera,
    Suppressed,
}

#[derive(Clone, Copy, Debug)]
pub(crate) struct Movement {
    pub target: MovementTarget,
    /// Unnormalized forward, left and up, each -1, 0 or 1.
    pub axes: [f64; 3],
    /// Any physical WASDQE key is down, even when opposing axes cancel.
    pub held: bool,
    /// Debug-camera speed only. Ship acceleration never uses this flag.
    pub boost: bool,
}

#[derive(Clone, Copy, Debug)]
pub(crate) struct PilotContext {
    pub active: bool,
    pub running: bool,
    pub can_control: bool,
    pub mission_running: bool,
    pub modal: bool,
}

#[derive(Clone, Copy, Debug, PartialEq)]
pub(crate) enum PilotEffect {
    None,
    SetCommand([f64; 3]),
    ReleaseCommand,
}
