"""Build a self-contained playback view from calibrated fuzz telemetry.

The view is deliberately ownship-only: time, position, velocity, attitude,
fuel, damage and commanded body vectors. It does not publish radar or private
connection data. Local points are the published formula on local counters.
"""

# ruff: noqa: E501

from __future__ import annotations

import argparse
import json
from pathlib import Path

PROFILES = {
    "ali_mppi": ("Ali · MPPI", "#1d4ed8"),
    "tom_polyfit_mvp": ("Tom · Polyfit", "#b45309"),
    "bui_alpha_beta_mvp": ("Bui · alpha-beta adapter", "#0f766e"),
    "basic_pd_radar": ("Harry · PD/radar", "#7c3aed"),
}
ORDER = tuple(PROFILES)


def load_profile(path: str | Path, profile: str) -> dict:
    report = json.loads(Path(path).read_text())
    runs = report.get("runs")
    if not isinstance(runs, list) or len(runs) != 1:
        raise ValueError(f"{path} must contain exactly one run")
    run = runs[0]
    telemetry = run.get("telemetry")
    if not isinstance(telemetry, dict) or not isinstance(telemetry.get("samples"), list):
        raise ValueError(f"{path} has no flight telemetry; rerun with telemetry enabled")
    if len(telemetry["samples"]) < 2:
        raise ValueError(f"{path} telemetry is too short")
    label, color = PROFILES[profile]
    return {
        "id": profile,
        "label": label,
        "color": color,
        "state": run.get("state"),
        "points": run.get("points"),
        "telemetry": telemetry,
    }


def build_dataset(paths: dict[str, str | Path], *, seed: int) -> dict:
    missing = [profile for profile in ORDER if profile not in paths]
    if missing:
        raise ValueError(f"missing profiles: {missing}")
    profiles = [load_profile(paths[profile], profile) for profile in ORDER]
    if any(profile["state"] is None for profile in profiles):
        raise ValueError("run state is missing")
    return {
        "schema": "wsorb.calibrated_flight_view",
        "schema_version": 1,
        "seed": seed,
        "duration_s": 300.0,
        "profiles": profiles,
    }


HTML = """<!doctype html>
<html lang="en">
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Calibrated v0.56 flight playback</title>
<style>
:root{color-scheme:light;--ink:#172033;--muted:#475569;--line:#dbe3ee;--bg:#f8fafc;--card:#fff}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font:14px/1.4 system-ui,-apple-system,"Segoe UI",sans-serif;font-variant-numeric:tabular-nums}
header{position:sticky;top:0;z-index:2;padding:14px 20px;background:#ffffffee;border-bottom:1px solid var(--line);backdrop-filter:blur(8px)}
h1{margin:0;font-size:20px;font-weight:650}h2{margin:0 0 6px;font-size:14px;font-weight:650}
p{margin:4px 0;color:var(--muted)}
.controls{display:flex;flex-wrap:wrap;gap:10px 14px;align-items:center;margin-top:10px}
button{border:1px solid var(--line);background:var(--card);color:var(--ink);border-radius:7px;padding:6px 10px;font:inherit;cursor:pointer}
button[aria-pressed="true"]{background:var(--ink);color:#fff;border-color:var(--ink)}
input[type=range]{width:min(520px,70vw);accent-color:#1d4ed8}
main{padding:16px 20px 28px;max-width:1440px;margin:0 auto}
.grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:14px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px;min-width:0}
.card-head{display:flex;align-items:center;gap:8px;margin-bottom:6px}
.dot{width:9px;height:9px;border-radius:50%}
.pill{margin-left:auto;border:1px solid var(--line);border-radius:999px;padding:2px 8px;font-size:12px;color:var(--muted)}
.plots{display:grid;grid-template-columns:1.25fr 1fr;gap:8px}
.plot{position:relative;min-width:0}.plot svg{display:block;width:100%;height:auto;background:#fff}
.plot span{position:absolute;left:6px;bottom:4px;font-size:11px;color:var(--muted)}
.stats{display:grid;grid-template-columns:repeat(3,1fr);gap:8px;margin:8px 0 0;padding:0;list-style:none}
.stats div{background:#f8fafc;border-radius:6px;padding:5px 7px}.stats b{display:block;font-size:15px}.stats span{font-size:11px;color:var(--muted)}
.charts{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:14px;margin-top:14px}
.panel{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px;min-width:0}
.panel svg{display:block;width:100%;height:auto}
#fuel{cursor:crosshair}
.slice{margin-top:14px}.slice h2{display:flex;gap:8px;align-items:baseline}
.slice table{width:100%;border-collapse:collapse;font-size:13px}
.slice th,.slice td{padding:5px 7px;border-bottom:1px solid var(--line);text-align:right}
.slice th:first-child,.slice td:first-child{text-align:left;color:var(--muted)}
.slice th{font-weight:650}.slice tr:last-child td{border-bottom:0}
.legend{display:flex;gap:12px;flex-wrap:wrap;font-size:12px;color:var(--muted);margin:4px 0 8px}
.hint{font-size:12px;color:var(--muted)}
.legend i{display:inline-block;width:9px;height:9px;border-radius:50%;margin-right:4px}
.note{font-size:12px;color:var(--muted);margin-top:12px}
@media (max-width:850px){.grid,.charts{grid-template-columns:1fr}.plots{grid-template-columns:1fr 1fr}}
</style>
<body>
<header>
  <h1>Calibrated v0.56 · seed __SEED__</h1>
  <p>Ownship telemetry at 2 Hz · fresh seed-104 capture · local points, not STP scores</p>
  <div class="controls">
    <button id="play" type="button">Play</button>
    <button id="reset" type="button">Reset</button>
    <label>t <output id="clock">0.0 s</output></label>
    <input id="scrub" type="range" min="0" max="300" step="0.1" value="0" aria-label="Flight time">
    <span id="speeds"></span>
    <span class="hint">hover charts or use ←/→</span>
  </div>
</header>
<main>
  <section class="grid" id="cards"></section>
  <section class="charts">
    <div class="panel"><h2>Terminal points</h2><svg id="points" viewBox="0 0 560 220" role="img" aria-label="Terminal points by controller"></svg></div>
    <div class="panel"><h2>Fuel used</h2><div class="legend" id="fuel-legend"></div><svg id="fuel" viewBox="0 0 560 220" role="img" aria-label="Fuel against time"></svg></div>
  </section>
  <section class="panel slice">
    <h2>Time slice <span id="slice-clock" class="pill">0.0 s</span></h2>
    <table id="slice" aria-label="Values at the selected time"></table>
  </section>
  <p class="note">Terminal points use the local formula at arrival. The cursor shows fuel, speed, range, altitude and thrust-axis attitude at the selected time. Dots freeze at each run’s terminal state; this is a fresh capture, so terminal points can differ from the five-seed table by timing.</p>
</main>
<script id="flight-data" type="application/json">__DATA__</script>
<script>
const DATA=JSON.parse(document.getElementById("flight-data").textContent);
const NS="http://www.w3.org/2000/svg";
const COL={success:"#15803d",damage:"#b91c1c",timeout:"#a16207"};
const el=(name,attrs={})=>{const n=document.createElementNS(NS,name);for(const[k,v]of Object.entries(attrs))n.setAttribute(k,v);return n};
const norm=v=>{const n=Math.hypot(...v);return n? v.map(x=>x/n):[0,0,0]};
const sub=(a,b)=>a.map((x,i)=>x-b[i]), dot=(a,b)=>a.reduce((s,x,i)=>s+x*b[i],0), cross=(a,b)=>[a[1]*b[2]-a[2]*b[1],a[2]*b[0]-a[0]*b[2],a[0]*b[1]-a[1]*b[0]];
const clamp=(x,a,b)=>Math.max(a,Math.min(b,x));
function rot(q,v){const[x,y,z,w]=q;const ix=w*v[0]+y*v[2]-z*v[1],iy=w*v[1]+z*v[0]-x*v[2],iz=w*v[2]+x*v[1]-y*v[0],iw=-x*v[0]-y*v[1]-z*v[2];return[ix*w+iw*-x+iy*-z-iz*-y,iy*w+iw*-y+iz*-x-ix*-z,iz*w+iw*-z+ix*-y-iy*-x]}
function basis(samples){const s=samples[0],p0=s[1],u=norm(sub(s[4],p0));let up=norm(p0);let e1=cross(up,u);if(Math.hypot(...e1)<1e-6)e1=[0,1,0];e1=norm(e1);const e2=cross(u,e1);return{p0,u,e1,e2}}
const local=(p,b)=>[dot(sub(p,b.p0),b.u),dot(sub(p,b.p0),b.e1),dot(sub(p,b.p0),b.e2)];
function pointsAt(s){const t=s[0],fuel=s[5],damage=s[6];return t<=0?0:100*(120/t-fuel/75-2*damage/209200+0.5)}
function sampleAt(samples,t){let lo=0,hi=samples.length-1;if(t<=samples[0][0])return samples[0];if(t>=samples[hi][0])return samples[hi];while(lo+1<hi){const mid=(lo+hi)>>1;if(samples[mid][0]<=t)lo=mid;else hi=mid}return samples[lo]}
const profiles=DATA.profiles.map(p=>{const b=basis(p.telemetry.samples);const path=p.telemetry.samples.map((s,i)=>{const att=local(rot(s[3],[0,0,1]),b);return{t:s[0],p:local(s[1],b),v:s[2],q:s[3],fuel:s[5],damage:s[6],points:pointsAt(s),speed:Math.hypot(...s[2]),range:Math.hypot(...sub(s[4],s[1])),att,attDeg:Math.acos(clamp(att[0],-1,1))*180/Math.PI}});return{...p,b,path,end:path[path.length-1].t,status:p.state==="native_terminal"?"arrived":p.state==="duration_limit"?(p.telemetry.samples.some(s=>s[6]>0)?"damage":"timeout"):p.state}});
const xMax=Math.max(...profiles.flatMap(p=>p.path.map(s=>s.p[0])),1305);
const yMax=Math.max(...profiles.flatMap(p=>p.path.map(s=>Math.abs(s.p[1]))),50)*1.15;
const zMin=Math.min(...profiles.flatMap(p=>p.path.map(s=>s.p[2])),0),zMax=Math.max(...profiles.flatMap(p=>p.path.map(s=>s.p[2])),50)*1.15;
const tMax=DATA.duration_s||300;
function pathAt(path,t){let lo=0,hi=path.length-1;if(t<=path[0].t)return path[0];if(t>=path[hi].t)return path[hi];while(lo+1<hi){const mid=(lo+hi)>>1;if(path[mid].t<=t)lo=mid;else hi=mid}return path[lo]}
function pathD(pts,sx,sy){return pts.map((q,i)=>`${i?"L":"M"}${sx(q.x).toFixed(1)},${sy(q.y).toFixed(1)}`).join("")}
function addText(svg,x,y,text,cls="",anchor="start"){const t=el("text",{x,y,class:cls,"text-anchor":anchor});t.textContent=text;svg.appendChild(t)}
function axes(svg,w,h,pad,x0,x1,y0,y1,labelX,labelY){const g=el("g",{stroke:"#e2e8f0","stroke-width":"1"});for(let i=0;i<=4;i++){const x=pad.l+(w-pad.l-pad.r)*i/4;g.appendChild(el("line",{x1:x,y1:pad.t,x2:x,y2:h-pad.b}))}for(let i=0;i<=3;i++){const y=pad.t+(h-pad.t-pad.b)*i/3;g.appendChild(el("line",{x1:pad.l,y1:y,x2:w-pad.r,y2:y}))}svg.appendChild(g);addText(svg,w/2,h-5,labelX);addText(svg,6,pad.t+10,labelY)}
function drawPath(svg,profile,card,t){const w=300,h=210,pad={l:34,r:10,t:8,b:24},sx=x=>pad.l+x/xMax*(w-pad.l-pad.r),sy=y=>h-pad.b-(y+yMax)/(2*yMax)*(h-pad.t-pad.b);axes(svg,w,h,pad,0,xMax,-yMax,yMax,"downrange, m","lateral");const pts=profile.path.filter(s=>s.t<=t).map(s=>({x:s.p[0],y:s.p[1]}));svg.appendChild(el("path",{d:pathD(pts,sx,sy),fill:"none",stroke:profile.color,"stroke-width":"2"}));const cur=pathAt(profile.path,t);const x=sx(cur.p[0]),y=sy(cur.p[1]);svg.appendChild(el("circle",{cx:x,cy:y,r:4,fill:profile.color}));const a=norm([cur.att[0],cur.att[1],0]);if(Math.hypot(...a)>0){svg.appendChild(el("line",{x1:x,y1:y,x2:x+a[0]*26,y2:y-a[1]*26,stroke:"#111827","stroke-width":"2"}))}const tx=sx(1305),ty=sy(0);svg.appendChild(el("circle",{cx:tx,cy:ty,r:7,fill:"none",stroke:"#64748b","stroke-dasharray":"3 3"}))}
function drawAlt(svg,profile,t){const w=240,h=210,pad={l:32,r:10,t:8,b:24},sx=x=>pad.l+x/xMax*(w-pad.l-pad.r),sy=z=>h-pad.b-(z-zMin)/(zMax-zMin)*(h-pad.t-pad.b);axes(svg,w,h,pad,0,xMax,zMin,zMax,"downrange, m","altitude");const pts=profile.path.filter(s=>s.t<=t).map(s=>({x:s.p[0],y:s.p[2]}));svg.appendChild(el("path",{d:pathD(pts,sx,sy),fill:"none",stroke:profile.color,"stroke-width":"2"}));const cur=pathAt(profile.path,t);const x=sx(cur.p[0]),y=sy(cur.p[2]);svg.appendChild(el("circle",{cx:x,cy:y,r:4,fill:profile.color}));const a=norm([cur.att[0],cur.att[2],0]);if(Math.hypot(...a)>0){svg.appendChild(el("line",{x1:x,y1:y,x2:x+a[0]*22,y2:y-a[1]*22,stroke:"#111827","stroke-width":"2"}))}}
const cards=document.getElementById("cards");
for(const p of profiles){const card=document.createElement("article");card.className="card";card.innerHTML=`<div class="card-head"><i class="dot" style="background:${p.color}"></i><b>${p.label}</b><span class="pill">${p.status}</span></div><div class="plots"><div class="plot"><svg class="top" viewBox="0 0 300 210"></svg><span>top-down · attitude</span></div><div class="plot"><svg class="alt" viewBox="0 0 240 210"></svg><span>altitude · attitude</span></div></div><ul class="stats"><li></li><li></li><li></li><li></li><li></li><li></li></ul>`;cards.appendChild(card);p.card=card;p.top=card.querySelector(".top");p.alt=card.querySelector(".alt");p.stats=card.querySelectorAll(".stats li")}
const psvg=document.getElementById("points"),fsvg=document.getElementById("fuel"),slice=document.getElementById("slice");
function series(max){const w=560,h=220,pad={l:36,r:10,t:10,b:24},sx=t=>pad.l+(t/tMax)*(w-pad.l-pad.r),sy=v=>h-pad.b-(v/max)*(h-pad.t-pad.b);return{t:sx,v:sy}}
function chartAxes(svg,xmax,ymax,labelX){const w=560,h=220,pad={l:36,r:10,t:10,b:24},g=el("g",{stroke:"#e2e8f0","stroke-width":"1"});for(let i=0;i<=4;i++){const x=pad.l+(w-pad.l-pad.r)*i/4;g.appendChild(el("line",{x1:x,y1:pad.t,x2:x,y2:h-pad.b}));addText(svg,x,h-14,(xmax*i/4).toFixed(0),"","middle")}for(let i=0;i<=4;i++){const y=pad.t+(h-pad.t-pad.b)*i/4;g.appendChild(el("line",{x1:pad.l,y1:y,x2:w-pad.r,y2:y}));addText(svg,pad.l-4,y+4,(ymax*(4-i)/4).toFixed(0),"","end")}svg.appendChild(g);addText(svg,w/2,h-2,labelX)}
const fs=series(140);
function renderPoints(){const w=560,padL=150,padR=50,y0=14,gap=48,barW=w-padL-padR;psvg.replaceChildren();profiles.forEach((p,i)=>{const y=y0+i*gap;addText(psvg,8,y+18,p.label.split(" · ")[0]);psvg.appendChild(el("rect",{x:padL,y,width:barW,height:26,rx:3,fill:"#e2e8f0"}));if(p.points!==null){psvg.appendChild(el("rect",{x:padL,y,width:barW*p.points/70,height:26,rx:3,fill:p.color}));addText(psvg,padL+barW+8,y+18,p.points.toFixed(1))}else{addText(psvg,padL+barW+8,y+18,"—")}})}
function drawSeries(svg,profile,map,t,key){const pts=profile.path.filter(s=>s.t<=t&&(key!=="points"||s.t>=5)).map(s=>({x:s.t,y:s[key]}));if(pts.length<2)return;svg.appendChild(el("path",{d:pts.map((q,i)=>`${i?"L":"M"}${map.t(q.x).toFixed(1)},${map.v(q.y).toFixed(1)}`).join(""),fill:"none",stroke:profile.color,"stroke-width":"2"}));const cur=pts[pts.length-1];svg.appendChild(el("circle",{cx:map.t(cur.x),cy:map.v(cur.y),r:3,fill:profile.color}))}
function renderSlice(t){document.getElementById("slice-clock").textContent=t.toFixed(1)+" s";const metrics=[["state",p=>t>=p.end-0.01?p.status:"flying",v=>v],["final points",p=>p.points,v=>v===null?"—":v.toFixed(1)],["fuel kg",p=>pathAt(p.path,t).fuel,v=>v.toFixed(1)],["speed m/s",p=>pathAt(p.path,t).speed,v=>v.toFixed(1)],["range m",p=>pathAt(p.path,t).range,v=>v.toFixed(1)],["alt m",p=>pathAt(p.path,t).p[2],v=>v.toFixed(1)],["att °",p=>pathAt(p.path,t).attDeg,v=>v.toFixed(1)]];slice.innerHTML=`<thead><tr><th>metric</th>${profiles.map(p=>`<th style="color:${p.color}">${p.label}</th>`).join("")}</tr></thead><tbody>${metrics.map(([name,value,format])=>`<tr><td>${name}</td>${profiles.map(p=>`<td>${format(value(p))}</td>`).join("")}</tr>`).join("")}</tbody>`}
function render(t){document.getElementById("clock").textContent=t.toFixed(1)+" s";document.getElementById("scrub").value=t;for(const p of profiles){p.top.replaceChildren();p.alt.replaceChildren();drawPath(p.top,p,p.card,t);drawAlt(p.alt,p,t);const s=pathAt(p.path,t);const vals=[p.points,s.fuel,s.speed,s.range,s.p[2],s.attDeg];const names=["final pts","fuel kg","speed m/s","range m","alt m","att °"];p.stats.forEach((li,i)=>{li.innerHTML=`<b>${vals[i]===null?"—":vals[i].toFixed(1)}</b><span>${names[i]}</span>`});p.card.querySelector(".pill").textContent=t>=p.end-0.01?p.status:"flying"}fsvg.replaceChildren();chartAxes(fsvg,tMax,140,"time, s");for(const p of profiles)drawSeries(fsvg,p,fs,t,"fuel");fsvg.appendChild(el("line",{x1:fs.t(t),y1:10,x2:fs.t(t),y2:196,stroke:"#94a3b8","stroke-dasharray":"3 3"}));renderSlice(t)}
document.getElementById("fuel-legend").innerHTML=profiles.map(p=>`<span><i style="background:${p.color}"></i>${p.label}</span>`).join("");
let t=0,playing=false,speed=1,last=0;
function setPlaying(v){playing=v;document.getElementById("play").textContent=v?"Pause":"Play"}
document.getElementById("play").onclick=()=>setPlaying(!playing);document.getElementById("reset").onclick=()=>{t=0;setPlaying(false);render(t)};
document.getElementById("scrub").oninput=e=>{t=+e.target.value;setPlaying(false);render(t)};
const speeds=document.getElementById("speeds");for(const s of [1,2,4,8]){const b=document.createElement("button");b.textContent=s+"×";b.setAttribute("aria-pressed",s===speed);b.onclick=()=>{speed=s;for(const x of speeds.children)x.setAttribute("aria-pressed",x===b)};speeds.appendChild(b)}
function chartTime(evt,svg){const r=svg.getBoundingClientRect(),padL=36,padR=10,x=((evt.clientX-r.left)/r.width)*560;return clamp((x-padL)/(560-padL-padR)*tMax,0,tMax)}
fsvg.addEventListener("pointermove",e=>{t=chartTime(e,fsvg);setPlaying(false);render(t)});
window.addEventListener("keydown",e=>{if(e.key!=="ArrowLeft"&&e.key!=="ArrowRight")return;e.preventDefault();t=clamp(t+(e.key==="ArrowRight"?0.5:-0.5),0,tMax);setPlaying(false);render(t)});
function frame(now){const dt=Math.min(0.1,(now-last)/1000);last=now;if(playing){t=Math.min(tMax,t+dt*speed);if(t>=tMax)setPlaying(false);render(t)}requestAnimationFrame(frame)}renderPoints();render(0);requestAnimationFrame(frame);
</script>
</body>
</html>"""


def render_html(dataset: dict) -> str:
    encoded = json.dumps(dataset, separators=(",", ":"), allow_nan=False)
    return HTML.replace("__SEED__", str(dataset["seed"])).replace("__DATA__", encoded)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    for profile in ORDER:
        parser.add_argument(f"--{profile}", type=Path, required=True)
    parser.add_argument("--seed", type=int, default=104)
    parser.add_argument("--json-out", type=Path, required=True)
    parser.add_argument("--html-out", type=Path, required=True)
    args = parser.parse_args(argv)
    paths = {profile: getattr(args, profile) for profile in ORDER}
    dataset = build_dataset(paths, seed=args.seed)
    args.json_out.parent.mkdir(parents=True, exist_ok=True)
    args.json_out.write_text(json.dumps(dataset, separators=(",", ":"), allow_nan=False) + "\n")
    args.html_out.write_text(render_html(dataset))
    print(args.html_out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
