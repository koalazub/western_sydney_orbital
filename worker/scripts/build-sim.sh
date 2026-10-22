#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
worker_dir="$(cd -- "$script_dir/.." && pwd)"
repo_root="$(cd -- "$worker_dir/.." && pwd)"
sim_dir="$worker_dir/public/sim"

cd "$repo_root"
cargo build --manifest-path simulation/Cargo.toml --locked --release --target wasm32-unknown-unknown
mkdir -p "$sim_dir"
wasm-bindgen \
  --target web \
  --out-name wsorb_bevy \
  --out-dir "$sim_dir" \
  "$repo_root/simulation/target/wasm32-unknown-unknown/release/wsorb_bevy.wasm"
