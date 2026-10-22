use bevy::{prelude::*, window::WindowResolution};
use serde::{Deserialize, Serialize};
use std::cell::RefCell;
use wasm_bindgen::prelude::*;

const MAX_STEP: f64 = 3600.0;
const EVENT_LIMIT: usize = 200;
const CRATERS: [(f64, f64, f64); 3] = [(25.0, 30.0, 6.0), (67.0, 60.0, 10.0), (80.0, 22.0, 4.0)];

#[derive(Clone, Serialize, Deserialize)]
struct Asset {
    id: String,
    name: String,
    x: f64,
    y: f64,
    battery_percent: f64,
    speed_mps: f64,
    sensor_radius_m: f64,
    battery_capacity_kwh: f64,
    #[serde(default = "default_move_power")]
    move_power_kw: f64,
    #[serde(default = "default_idle_power")]
    idle_power_kw: f64,
    status: String,
    #[serde(default)]
    destination: Option<[f64; 2]>,
}
#[derive(Clone, Serialize, Deserialize)]
struct Event { t_s: f64, asset: String, message: String }
#[derive(Clone, Serialize, Deserialize)]
struct Snapshot {
    time_s: f64, seed: u64, found: bool, reported: bool,
    bounds: Bounds, assets: Vec<Asset>, events: Vec<Event>,
    #[serde(default)] target: Option<[f64; 2]>,
    #[serde(default)] detected: bool,
}
#[derive(Clone, Serialize, Deserialize)]
struct Bounds { min_x: f64, max_x: f64, min_y: f64, max_y: f64 }
fn default_move_power()->f64{2.0}
fn default_idle_power()->f64{0.1}
impl Default for Snapshot {
    fn default() -> Self {
        // Scout1 is first because the Python example selects assets[0] by default.
        let names = ["Scout1", "LTV1", "LTV2", "Scout2", "TruckRover", "ExcavatorRover", "SamplingRover"];
        Self {
            time_s: 0.0, seed: 1, found: false, reported: false,
            bounds: Bounds { min_x: 0.0, max_x: 100.0, min_y: 0.0, max_y: 100.0 },
            assets: names.iter().enumerate().map(|(i, n)| Asset {
                id: (*n).into(), name: (*n).into(), x: 12.0 + (i % 4) as f64 * 7.0,
                y: 12.0 + (i / 4) as f64 * 8.0, battery_percent: 100.0,
                speed_mps: if i == 0 || i == 3 { 1.8 } else { 0.8 }, sensor_radius_m: if i == 0 || i == 3 { 12.0 } else { 6.0 },
                battery_capacity_kwh: 80.0, move_power_kw: 2.0, idle_power_kw: 0.1,
                status: "idle".into(), destination: None,
            }).collect(), events: vec![], target: Some([38.0, 24.0]), detected: false,
        }
    }
}
thread_local! { static STATE: RefCell<Snapshot> = RefCell::new(Snapshot::default()); }

#[wasm_bindgen]
pub fn command(json: &str) -> String {
    let result = (|| -> Result<Snapshot, String> {
        let v: serde_json::Value = serde_json::from_str(json).map_err(|e| e.to_string())?;
        let op = v["op"].as_str().ok_or("missing op")?;
        STATE.with(|cell| {
            let mut s = cell.borrow_mut();
            match op {
                "reset" => { let seed = v["seed"].as_u64().unwrap_or(1); *s = Snapshot::default(); s.seed = seed; s.target=Some(target_for_seed(seed)); }
                "restore" => { let mut restored:Snapshot=serde_json::from_value(v["state"].clone()).map_err(|e| e.to_string())?; validate_snapshot(&restored)?; if !restored.found || restored.target.is_none() { restored.target=Some(target_for_seed(restored.seed)); } *s=restored; }
                "observe" => {},
                "report" => { if !s.found { return Err("no target has been found to report".into()); } s.reported=true; let now=s.time_s; push_event(&mut s,Event{t_s:now,asset:"Team".into(),message:"Target report transmitted".into()}); }
                "step" => {
                    let dt = number(&v["seconds"])?;
                    if dt < 0.0 || dt > MAX_STEP { return Err(format!("seconds must be between 0 and {MAX_STEP}")); }
                    advance(&mut s, dt);
                }
                "move_to" => {
                    let id = v["entity"].as_str().ok_or("missing entity")?;
                    let x = number(&v["x"])?; let y = number(&v["y"])?;
                    if x < s.bounds.min_x || x > s.bounds.max_x || y < s.bounds.min_y || y > s.bounds.max_y { return Err("destination outside map bounds".into()); }
                    let a = s.assets.iter_mut().find(|a| a.id == id).ok_or("unknown entity")?;
                    if a.battery_percent <= 0.0 { return Err("asset battery is depleted; restore its battery before routing".into()); }
                    if segment_hits_crater(a.x,a.y,x,y) { return Err("route crosses a crater keep-out; choose a waypoint around the crater".into()); }
                    a.destination = Some([x,y]); a.status = "moving".into();
                    let now=s.time_s;
                    push_event(&mut s,Event {t_s:now, asset:id.into(), message:format!("Route set to ({x:.1}, {y:.1})")});
                }
                "stop" => { let id=v["entity"].as_str().ok_or("missing entity")?; let a=s.assets.iter_mut().find(|a|a.id==id).ok_or("unknown entity")?; a.destination=None; a.status="idle".into(); }
                "set_attribute" => {
                    let id=v["entity"].as_str().ok_or("missing entity")?; let attr=v["attribute"].as_str().ok_or("missing attribute")?; let val=number(&v["value"])?;
                    let a=s.assets.iter_mut().find(|a|a.id==id).ok_or("unknown entity")?;
                    match attr { "speed_mps" if val>0.0 && val<=10.0=>a.speed_mps=val, "sensor_radius_m" if val>0.0 && val<=100.0=>a.sensor_radius_m=val, "battery_capacity_kwh" if val>0.0 && val<=1000.0=>a.battery_capacity_kwh=val, "battery_percent" if (0.0..=100.0).contains(&val)=>a.battery_percent=val, "move_power_kw" if val>0.0 && val<=100.0=>a.move_power_kw=val, "idle_power_kw" if (0.0..=10.0).contains(&val)=>a.idle_power_kw=val, _=>return Err("invalid attribute or value".into()) }
                }
                _ => return Err(format!("unknown operation: {op}")),
            }
            Ok(s.clone())
        })
    })();
    match result { Ok(mut s)=>{ if !s.found { s.target=None; } serde_json::json!({"ok":true,"state":s}).to_string() }, Err(e)=>serde_json::json!({"ok":false,"error":e}).to_string() }
}
fn number(v:&serde_json::Value)->Result<f64,String>{ let n=v.as_f64().ok_or("expected finite number")?; if n.is_finite(){Ok(n)}else{Err("expected finite number".into())} }
fn push_event(s:&mut Snapshot,event:Event){s.events.push(event);if s.events.len()>EVENT_LIMIT{s.events.drain(..s.events.len()-EVENT_LIMIT);}}
fn target_for_seed(seed:u64)->[f64;2]{
    let mut state=if seed==0{0x9e3779b97f4a7c15}else{seed};
    for _ in 0..128 {
        state^=state<<13;state^=state>>7;state^=state<<17;
        let x=8.0+(state%8401) as f64/100.0;
        state=state.wrapping_add(0x9e3779b97f4a7c15);
        let y=8.0+(state%8401) as f64/100.0;
        let spawn_clear=Snapshot::default().assets.iter().all(|a| ((x-a.x).powi(2)+(y-a.y).powi(2)).sqrt()>15.0);
        let crater_clear=CRATERS.iter().all(|(cx,cy,r)|((x-cx).powi(2)+(y-cy).powi(2)).sqrt()>r+3.0);
        if spawn_clear&&crater_clear{return [x,y];}
    }
    [92.0,92.0]
}
fn validate_snapshot(s:&Snapshot)->Result<(),String>{
    if !s.time_s.is_finite()||s.time_s<0.0{return Err("invalid snapshot clock".into());}
    let b=&s.bounds;
    if ![b.min_x,b.max_x,b.min_y,b.max_y].iter().all(|v|v.is_finite())||b.min_x>=b.max_x||b.min_y>=b.max_y{return Err("invalid snapshot bounds".into());}
    if s.assets.len()>7{return Err("snapshot has more than seven assets".into());}
    let mut ids=std::collections::HashSet::new();
    for a in &s.assets {
        if a.id.is_empty()||!ids.insert(&a.id){return Err("snapshot asset IDs must be non-empty and unique".into());}
        if !a.x.is_finite()||!a.y.is_finite()||a.x<b.min_x||a.x>b.max_x||a.y<b.min_y||a.y>b.max_y{return Err(format!("invalid position for {}",a.id));}
        if !a.battery_percent.is_finite()||!(0.0..=100.0).contains(&a.battery_percent)||!a.battery_capacity_kwh.is_finite()||a.battery_capacity_kwh<=0.0||!a.speed_mps.is_finite()||a.speed_mps<=0.0||!a.sensor_radius_m.is_finite()||a.sensor_radius_m<=0.0||!a.move_power_kw.is_finite()||a.move_power_kw<=0.0||!a.idle_power_kw.is_finite()||a.idle_power_kw<0.0{return Err(format!("invalid attributes for {}",a.id));}
        if let Some([x,y])=a.destination {if !x.is_finite()||!y.is_finite()||x<b.min_x||x>b.max_x||y<b.min_y||y>b.max_y||segment_hits_crater(a.x,a.y,x,y){return Err(format!("invalid destination for {}",a.id));}}
    }
    if s.events.len()>EVENT_LIMIT{return Err(format!("snapshot event log exceeds {EVENT_LIMIT} entries"));}
    if s.events.iter().any(|e|!e.t_s.is_finite()||e.t_s<0.0){return Err("invalid event time in snapshot".into());}
    if let Some([x,y])=s.target{if !x.is_finite()||!y.is_finite()||x<b.min_x||x>b.max_x||y<b.min_y||y>b.max_y{return Err("invalid target position in snapshot".into());}}
    if s.reported&&!s.found{return Err("snapshot cannot report a target that has not been found".into());}
    Ok(())
}
fn segment_hits_crater(x1:f64,y1:f64,x2:f64,y2:f64)->bool{
    CRATERS.iter().any(|(cx,cy,r)|{
        let dx=x2-x1;let dy=y2-y1;let length2=dx*dx+dy*dy;
        let t=if length2==0.0{0.0}else{(((cx-x1)*dx+(cy-y1)*dy)/length2).clamp(0.0,1.0)};
        let px=x1+t*dx;let py=y1+t*dy;(px-cx).powi(2)+(py-cy).powi(2)<=r*r
    })
}
fn advance(s:&mut Snapshot, dt:f64) {
    s.time_s += dt;
    let mut paths=Vec::new();
    for a in &mut s.assets {
        let from=[a.x,a.y];let available=a.battery_capacity_kwh*a.battery_percent/100.0;
        let idle_energy=(a.idle_power_kw*dt/3600.0).min(available);
        let mut remaining=(available-idle_energy).max(0.0);
        if let Some(d)=a.destination {
            let dx=d[0]-a.x;let dy=d[1]-a.y;let dist=(dx*dx+dy*dy).sqrt();
            let possible_time=(remaining*3600.0/a.move_power_kw).min(dt);
            let travel=(a.speed_mps*possible_time).min(dist);
            let moving_time=if a.speed_mps>0.0{travel/a.speed_mps}else{0.0};
            if dist>0.0{a.x+=dx/dist*travel;a.y+=dy/dist*travel;}
            let consumed=a.move_power_kw*moving_time/3600.0;remaining=(remaining-consumed).max(0.0);
            if dist<=travel+f64::EPSILON{a.destination=None;a.status="idle".into();}
        }
        let final_energy=remaining;
        a.battery_percent=(final_energy/a.battery_capacity_kwh*100.0).clamp(0.0,100.0);
        if final_energy<=f64::EPSILON {a.destination=None;a.status="depleted".into();}
        paths.push((a.id.clone(),from,[a.x,a.y],a.sensor_radius_m));
    }
    if !s.detected {if let Some([tx,ty])=s.target {if paths.iter().any(|(_,from,to,r)|point_segment_distance(tx,ty,*from,*to)<=*r){s.detected=true;s.found=true;push_event(s,Event{t_s:s.time_s,asset:"Team".into(),message:"Signal detected; target location identified".into()});}}}
}
fn point_segment_distance(px:f64,py:f64,a:[f64;2],b:[f64;2])->f64{
    let dx=b[0]-a[0];let dy=b[1]-a[1];let l=dx*dx+dy*dy;let t=if l==0.0{0.0}else{(((px-a[0])*dx+(py-a[1])*dy)/l).clamp(0.0,1.0)};
    ((px-(a[0]+t*dx)).powi(2)+(py-(a[1]+t*dy)).powi(2)).sqrt()
}
#[derive(Component)] struct Rover(String);
#[derive(Component)] struct TargetMarker;
fn sync_scene(mut query:Query<(&Rover,&mut Transform)>,mut target_query:Query<(&mut Transform,&mut Visibility),With<TargetMarker>>) {
    STATE.with(|cell| {
        let state=cell.borrow();
        for (rover,mut transform) in &mut query {
            if let Some(asset)=state.assets.iter().find(|a|a.id==rover.0) {
                transform.translation.x=asset.x as f32;
                transform.translation.z=asset.y as f32;
            }
        }
        for (mut transform,mut visibility) in &mut target_query {
            if let Some([x,y])=state.target {transform.translation.x=x as f32;transform.translation.z=y as f32;}
            *visibility=if state.found{Visibility::Visible}else{Visibility::Hidden};
        }
    });
}
fn setup(mut commands:Commands, mut meshes:ResMut<Assets<Mesh>>, mut mats:ResMut<Assets<StandardMaterial>>) {
    commands.spawn((Camera3d::default(), Transform::from_xyz(65.0,75.0,95.0).looking_at(Vec3::new(50.0,0.0,50.0),Vec3::Y)));
    commands.spawn((DirectionalLight::default(),Transform::from_xyz(20.0,40.0,10.0).looking_at(Vec3::ZERO,Vec3::Y)));
    commands.spawn((Mesh3d(meshes.add(Plane3d::default().mesh().size(100.0,100.0))),MeshMaterial3d(mats.add(Color::srgb(0.35,0.32,0.29))),Transform::from_xyz(50.0,0.0,50.0)));
    for (i,(x,y,r)) in CRATERS.iter().enumerate() {
        commands.spawn((Mesh3d(meshes.add(Cylinder::new(*r as f32,0.3))),MeshMaterial3d(mats.add(Color::srgb(0.22,0.20,0.19))),Transform::from_xyz(*x as f32,0.16,*y as f32),Rover(format!("crater{i}"))));
    }
    for (i,(x,y)) in [(15.,80.),(50.,50.),(85.,80.)].iter().enumerate() {
        commands.spawn((Mesh3d(meshes.add(Cuboid::new(2.0,3.0,2.0))),MeshMaterial3d(mats.add(Color::srgb(0.2,0.8,0.9))),Transform::from_xyz(*x as f32,1.5,*y as f32),Rover(format!("relay{i}"))));
    }
    let s=STATE.with(|v|v.borrow().clone());
    for (i,a) in s.assets.iter().enumerate() { let color=Color::srgb(0.95-i as f32*0.08,0.65,0.22+i as f32*0.07);
        commands.spawn((Mesh3d(meshes.add(Cuboid::new(2.8,1.3,3.8))),MeshMaterial3d(mats.add(color)),Transform::from_xyz(a.x as f32,0.8,a.y as f32),Rover(a.id.clone())));
    }
    commands.spawn((Mesh3d(meshes.add(Sphere::new(1.8).mesh().ico(2).unwrap())),MeshMaterial3d(mats.add(Color::srgb(0.95,0.9,0.2))),Transform::from_xyz(0.0,2.0,0.0),Visibility::Hidden,TargetMarker));
}

#[wasm_bindgen]
pub fn start_simulation(canvas_selector: &str) {
    let selector=canvas_selector.to_string();
    App::new().add_plugins(DefaultPlugins.set(WindowPlugin { primary_window:Some(Window { canvas:Some(selector), fit_canvas_to_parent:true, resolution:WindowResolution::new(1000,700), ..default() }), ..default() })).add_systems(Startup,setup).add_systems(Update,sync_scene).run();
}
