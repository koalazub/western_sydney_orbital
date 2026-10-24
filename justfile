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

# Format the repository's Python, Rust and Nix sources together.
format: format-nix
    devenv shell uv run --locked ruff format .
    nix shell --impure .#rust --command cargo fmt --manifest-path simulation/Cargo.toml

format-nix:
    nix fmt

format-module path:
    nix shell --impure .#rust --command rustfmt --edition 2024 {{quote(path)}}

# Fast integrated feedback while modules or dependency features are changing.
compile:
    nix shell --impure .#rust --command cargo check --manifest-path simulation/Cargo.toml --target wasm32-unknown-unknown --lib

unit-check:
    nix shell --impure .#game --command cargo test --manifest-path simulation/Cargo.toml --locked --lib
    devenv tasks run wsorb:test

# Compile and lint the actual engine and bundle the thin browser launcher.
check:
    devenv tasks run wsorb:lint
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

cf-network-check:
    #!/usr/bin/env bash
    set -euo pipefail
    nix shell --impure .#game --command node --input-type=module <<'JS'
    import {resolve4,resolve6} from 'node:dns/promises';
    console.log({ipv4:await resolve4('api.cloudflare.com').catch(String),ipv6:await resolve6('api.cloudflare.com').catch(String)});
    for(const url of ['https://api.cloudflare.com/client/v4/user','https://western-sydney-orbital-api.western-sydney-orbital-worker.workers.dev/sim/manifest.json']) {
      const start=Date.now();
      try {const response=await fetch(url,{signal:AbortSignal.timeout(15000)});await response.arrayBuffer();console.log({url,status:response.status,ms:Date.now()-start});}
      catch(error){console.log({url,error:String(error),cause: String(error.cause),code:error.cause?.code,errors:error.cause?.errors?.map(e=>({message:e.message,code:e.code,address:e.address}))});process.exitCode=1;}
    }
    JS

cf-network-debug:
    WSO_CF_NETWORK_DIAGNOSTICS=1 nix shell --impure .#game --command npm --prefix worker run wrangler -- deployments status

cf-http2-check:
    WSO_CF_HTTP2=1 WSO_CF_NETWORK_DIAGNOSTICS=1 nix shell --impure .#game --command npm --prefix worker run wrangler -- deployments status

# Pin the optional CLI transport dependency without upgrading Wrangler.
setup-cloudflare-network:
    nix shell --impure .#game --command npm --prefix worker install --save-dev --save-exact undici@7.29.1

cf-processes:
    nix shell --impure .#game --command node --input-type=module -e 'import {execFileSync} from "node:child_process"; const root=process.cwd()+"/worker/node_modules/"; console.log(execFileSync("ps",["-axo","pid=,ppid=,etime=,command="],{encoding:"utf8"}).split("\n").filter(line=>line.includes(root)&&/\bdeploy\b/.test(line)).join("\n")||"No project deployment processes remain.");'

deploy-help:
    nix shell --impure .#game --command npm --prefix worker run wrangler -- deploy --help

deployment-status:
    nix shell --impure .#game --command npm --prefix worker run wrangler -- deployments status

deploy-dry-run:
    nix shell --impure .#game --command npm --prefix worker run wrangler -- deploy --dry-run

hosted-artifact-check:
    #!/usr/bin/env bash
    set -euo pipefail
    nix shell --impure .#game --command node --input-type=module <<'JS'
    import assert from 'node:assert/strict';
    import {createHash} from 'node:crypto';
    import {readFile} from 'node:fs/promises';
    const base='https://western-sydney-orbital-api.western-sydney-orbital-worker.workers.dev/';
    const expected=JSON.parse(await readFile('worker/public/sim/manifest.json','utf8'));
    const request=path=>fetch(new URL(path,base),{cache:'no-store',signal:AbortSignal.timeout(60000)});
    const manifestResponse=await request('sim/manifest.json');
    assert.equal(manifestResponse.status,200);
    const manifest=await manifestResponse.json();
    assert.deepEqual(manifest,expected);
    const response=await request(`sim/wsorb_bevy_bg.wasm?sha256=${expected.sha256}`);
    assert.equal(response.status,200);
    assert.match(response.headers.get('content-type'),/^application\/wasm(?:;|$)/);
    const bytes=Buffer.from(await response.arrayBuffer());
    const sha256=createHash('sha256').update(bytes).digest('hex');
    assert.equal(bytes.length,expected.bytes);
    assert.equal(sha256,expected.sha256);
    console.log(JSON.stringify({url:base,bytes:bytes.length,sha256,result:'PASS'},null,2));
    JS

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

# Owner-controlled release only. Never a dependency of local/check.
deploy:
    nix shell --impure .#game --command npm --prefix worker run deploy

# Per-process fallback for intermittent dual-stack connection failures.
deploy-ipv4:
    NODE_OPTIONS="${NODE_OPTIONS:-} --dns-result-order=ipv4first" nix shell --impure .#game --command npm --prefix worker run deploy

deploy-debug:
    WSO_CF_NETWORK_DIAGNOSTICS=1 nix shell --impure .#game --command npm --prefix worker run deploy

deploy-http2:
    WSO_CF_HTTP2=1 WSO_CF_NETWORK_DIAGNOSTICS=1 nix shell --impure .#game --command npm --prefix worker run deploy

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
