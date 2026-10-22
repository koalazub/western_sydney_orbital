#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
worker_dir="$(cd -- "$script_dir/.." && pwd)"
repo_root="$(cd -- "$worker_dir/.." && pwd)"
sim_dir="$worker_dir/public/sim"
stage_dir="$(mktemp -d "${TMPDIR:-/tmp}/wsorb-build.XXXXXX")"
trap 'rm -rf "$stage_dir"' EXIT

cd "$repo_root"
cargo build --manifest-path simulation/Cargo.toml --locked --release --target wasm32-unknown-unknown
wasm-bindgen \
  --target web \
  --out-name wsorb_bevy \
  --out-dir "$stage_dir" \
  "$repo_root/simulation/target/wasm32-unknown-unknown/release/wsorb_bevy.wasm"
wasm-opt -Oz --all-features --strip-debug \
  "$stage_dir/wsorb_bevy_bg.wasm" -o "$stage_dir/wsorb_bevy_bg.optimized.wasm"
mv "$stage_dir/wsorb_bevy_bg.optimized.wasm" "$stage_dir/wsorb_bevy_bg.wasm"
node --input-type=module - "$stage_dir" <<'JS'
import { readFile, writeFile } from 'node:fs/promises';
import { createHash } from 'node:crypto';
import { join } from 'node:path';
const directory = process.argv[2];
const bytes = await readFile(join(directory, 'wsorb_bevy_bg.wasm'));
await writeFile(join(directory, 'manifest.json'), JSON.stringify({
  bytes: bytes.byteLength,
  sha256: createHash('sha256').update(bytes).digest('hex'),
}) + '\n');
JS
mkdir -p "$sim_dir"
cp "$stage_dir"/* "$sim_dir/"
