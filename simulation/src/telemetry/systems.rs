use super::{data::Tracker, presentation::publish, tracking};
use crate::EngineSet;
use bevy::prelude::*;

pub(crate) fn install(app: &mut App) {
    app.init_resource::<Tracker>().add_systems(
        Update,
        update.in_set(EngineSet::Reports).after(EngineSet::Runtime),
    );
}

fn update(mut tracker: ResMut<Tracker>) {
    let snapshot = crate::snapshot();
    publish(tracking::advance(&mut tracker, &snapshot));
}
