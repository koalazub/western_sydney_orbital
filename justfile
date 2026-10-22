set shell := ["bash", "-euo", "pipefail", "-c"]

default:
    @just --list

environment-update:
    devenv update
    nix flake update

# Prefer HTTP/3 where the linked curl supports QUIC; keep negotiated fallback.
environment-bootstrap:
    devenv --nix-option http3 true shell true

# Isolate protocol-negotiation failures without changing global Nix settings.
environment-http1-check:
    devenv --nix-option http2 false --nix-option http3 false shell true

http-capabilities:
    nix --version
    nix config show http3

# Verify the flake's native Devenv shell through Nix's develop path.
environment-build:
    nix develop --impure --command true

# Evaluate every declared OS/architecture without claiming foreign builds ran.
platform-check:
    nix flake check --all-systems --no-build --impure

# Install the game dependencies without downloading optional Python/dev tools.
setup:
    nix shell --impure .#game --command npm ci --prefix worker

setup-python:
    devenv shell uv sync --locked

# Format the Rust modules before checking or building them.
format:
    nix shell --impure .#rust --command cargo fmt --manifest-path simulation/Cargo.toml

format-nix:
    nix fmt

format-module path:
    nix shell --impure .#rust --command rustfmt --edition 2024 {{quote(path)}}

# Fast integrated feedback while modules or dependency features are changing.
compile:
    nix shell --impure .#rust --command cargo check --manifest-path simulation/Cargo.toml --target wasm32-unknown-unknown --lib

# Compile and lint the actual engine and bundle the thin browser launcher.
check:
    nix shell --impure .#rust --command cargo fmt --manifest-path simulation/Cargo.toml -- --check
    nix shell --impure .#game --command cargo clippy --manifest-path simulation/Cargo.toml --locked --target wasm32-unknown-unknown --lib -- -D warnings
    nix shell --impure .#game --command npm --prefix worker run build:web
    git diff --check

# Build the WebGPU engine, optimize WASM and prepare browser assets.
build:
    nix shell --impure .#game --command npm --prefix worker run build

artifact-size:
    nix shell --impure .#game --command node --input-type=module -e 'import {readFileSync} from "node:fs"; import {gzipSync,brotliCompressSync} from "node:zlib"; const b=readFileSync("worker/public/sim/wsorb_bevy_bg.wasm"); console.log(JSON.stringify({wasm_bytes:b.length,gzip_bytes:gzipSync(b).length,brotli_bytes:brotliCompressSync(b).length},null,2)); if(b.length>25*1024*1024)process.exitCode=1;'

web-build:
    nix shell --impure .#game --command npm --prefix worker run build:web

# Remove editor-only dependencies from the canvas-only browser application.
web-cleanup:
    nix shell --impure .#game --command npm --prefix worker uninstall @codemirror/lang-python codemirror y-codemirror.next y-protocols

# Run the built game locally. This never deploys to Cloudflare.
local: build
    nix shell --impure .#game --command npm --prefix worker run local

local-check:
    #!/usr/bin/env bash
    set -euo pipefail
    nix shell --impure .#game --command node --input-type=module <<'JS'
    import {spawn} from 'node:child_process';
    const child=spawn(process.execPath,['worker/scripts/local.mjs'],{env:{...process.env,PORT:'0'},stdio:['ignore','pipe','inherit']});
    try {
      const url=await new Promise((resolve,reject)=>{child.once('error',reject);child.stdout.on('data',b=>{const match=String(b).match(/http:\/\/127\.0\.0\.1:\d+\//);if(match)resolve(match[0]);});setTimeout(()=>reject(new Error('Local startup timed out')),5000).unref();});
      const page=await fetch(url), manifest=await (await fetch(new URL('sim/manifest.json',url))).json();
      const wasm=await fetch(new URL('sim/wsorb_bevy_bg.wasm',url),{method:'HEAD'});
      const denied=await fetch(url,{method:'POST'});
      if(page.status!==200||wasm.status!==200||Number(wasm.headers.get('content-length'))!==manifest.bytes||denied.status!==405||page.headers.get('x-content-type-options')!=='nosniff')throw new Error('Local HTTP contract failed');
      console.log(JSON.stringify({url,page:page.status,wasm:wasm.status,bytes:manifest.bytes,writeRejected:denied.status,result:'PASS'}));
    } finally {child.kill('SIGTERM');}
    JS

# Observe real mouse, keyboard, native HUD and fullscreen behavior in Chrome.
preview:
    PREVIEW_OUTPUT_DIR="${TMPDIR:-/tmp}/opencode/wso-controls" nix shell --impure .#game --command npm --prefix worker run preview -- --controls --headed

platform-browser-check:
    PREVIEW_OUTPUT_DIR="${TMPDIR:-/tmp}/opencode/wso-platform" nix shell --impure .#game --command npm --prefix worker run preview -- --platform

headed-platform-check:
    PREVIEW_OUTPUT_DIR="${TMPDIR:-/tmp}/opencode/wso-headed-platform" nix shell --impure .#game --command npm --prefix worker run preview -- --platform --headed

preview-artifacts name:
    PREVIEW_PARENT="${TMPDIR:-/tmp}/opencode" nix shell --impure .#game --command node --input-type=module -e 'import {readdirSync,statSync} from "node:fs"; import {join} from "node:path"; const d=join(process.env.PREVIEW_PARENT,process.argv[1]); console.log(readdirSync(d).map(name=>({name,time:statSync(join(d,name)).mtime.toISOString(),bytes:statSync(join(d,name)).size})).sort((a,b)=>b.time.localeCompare(a.time)));' {{quote(name)}}

mission-check:
    PREVIEW_OUTPUT_DIR="${TMPDIR:-/tmp}/opencode/wso-mission" nix shell --impure .#game --command npm --prefix worker run preview -- --run-mission

small-screen-check:
    PREVIEW_OUTPUT_DIR="${TMPDIR:-/tmp}/opencode/wso-small-screen" nix shell --impure .#game --command npm --prefix worker run preview -- --width=320 --height=900 --controls --headed

download-failure-check:
    PREVIEW_OUTPUT_DIR="${TMPDIR:-/tmp}/opencode/wso-download-failure" nix shell --impure .#game --command npm --prefix worker run preview -- --block=wasm

tools:
    nix shell --impure .#game --command rustc --version
    nix shell --impure .#game --command cargo --version
    nix shell --impure .#game --command wasm-bindgen --version
    nix shell --impure .#game --command wasm-opt --version

# Inspect the devenv-selected compiler without unrelated shell packages.
rust-version:
    nix shell --impure .#rust --command rustc --version

game-tools:
    nix shell --impure .#game --command node --version
    nix shell --impure .#game --command wasm-bindgen --version
    nix shell --impure .#game --command wasm-opt --version

game-tool-paths:
    nix derivation show --recursive --impure .#game

task-info id:
    bd show {{quote(id)}} --json --sandbox

account:
    nix shell --impure .#game --command npm --prefix worker run account

deploy-help:
    nix shell --impure .#game --command npm --prefix worker run wrangler -- deploy --help

deployment-status:
    nix shell --impure .#game --command npm --prefix worker run wrangler -- deployments status

deploy-dry-run:
    nix shell --impure .#game --command npm --prefix worker run wrangler -- deploy --dry-run

hosted-check:
    PREVIEW_OUTPUT_DIR="${TMPDIR:-/tmp}/opencode/wso-hosted-controls" nix shell --impure .#game --command npm --prefix worker run preview -- --controls --headed --url=https://western-sydney-orbital-api.western-sydney-orbital-worker.workers.dev/

hosted-mission-check:
    PREVIEW_OUTPUT_DIR="${TMPDIR:-/tmp}/opencode/wso-hosted-mission" nix shell --impure .#game --command npm --prefix worker run preview -- --run-mission --url=https://western-sydney-orbital-api.western-sydney-orbital-worker.workers.dev/

disable-ci:
    gh api --method PUT repos/koalazub/western_sydney_orbital/actions/permissions -F enabled=false

ci-status:
    gh api repos/koalazub/western_sydney_orbital/actions/permissions --jq .enabled

repository-status:
    gh repo view --json nameWithOwner,isPrivate,defaultBranchRef,url

login-cf:
    nix shell --impure .#game --command npm --prefix worker run login:cf

login-wrangler:
    nix shell --impure .#game --command npm --prefix worker run login:wrangler

# Owner-controlled release only. Never a dependency of local/check/preview.
deploy:
    nix shell --impure .#game --command npm --prefix worker run deploy

tasks:
    bd list --status open --json --sandbox

task-create title description parent assignee:
    bd create {{quote(title)}} --description {{quote(description)}} --parent {{quote(parent)}} --assignee {{quote(assignee)}} --type task --priority 0 --status open --silent --sandbox

task-assign id assignee:
    bd update {{quote(id)}} --assignee {{quote(assignee)}} --status in_progress --sandbox

task-block id reason:
    bd update {{quote(id)}} --status blocked --notes {{quote(reason)}} --sandbox

task-depend id dependency:
    bd dep add {{quote(id)}} {{quote(dependency)}} --sandbox

task-note id text:
    bd comments add {{quote(id)}} {{quote(text)}} --sandbox

task-close id reason:
    bd close {{quote(id)}} --reason {{quote(reason)}} --sandbox

# Coordinator completion after an assigned subagent has returned and been reviewed.
task-finish id reason:
    bd close {{quote(id)}} --reason {{quote(reason)}} --force --sandbox

beads-sync:
    bd export -o .beads/issues.jsonl --sandbox
    GIT_AUTHOR_NAME="Beads Sync" GIT_AUTHOR_EMAIL="beads-sync@users.noreply.github.com" GIT_COMMITTER_NAME="Beads Sync" GIT_COMMITTER_EMAIL="beads-sync@users.noreply.github.com" bd dolt push --remote origin --no-adopt --sandbox

status:
    jj status

review:
    git diff --check
    jj diff --stat

diff +paths:
    jj diff --git {{paths}}

history:
    jj log -r 'ancestors(@, 7)' --no-pager

describe revision message:
    jj describe -r {{quote(revision)}} -m {{quote(message)}}

commit message +paths:
    jj commit -m {{quote(message)}} {{paths}}

push:
    jj bookmark set main -r @-
    jj git push --bookmark main
