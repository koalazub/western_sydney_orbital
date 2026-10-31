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

# Keep the VM app separate from the development shell and preserve existing apps.
utm-install:
    #!/usr/bin/env bash
    set -euo pipefail
    if [[ "$(uname -s)" != Darwin ]]; then
        echo 'UTM is only available on macOS.' >&2
        exit 1
    fi
    app="$HOME/Applications/UTM.app"
    source="$PWD/.devenv/utm/Applications/UTM.app"
    if [[ -e "$app" || -L "$app" ]]; then
        if [[ ! -L "$app" || "$(readlink "$app")" != "$source" ]]; then
            echo "Refusing to replace an existing app: $app" >&2
            exit 1
        fi
    fi
    mkdir -p .devenv "$HOME/Applications"
    nix build --impure .#utm --out-link .devenv/utm
    test -d "$source"
    if [[ ! -L "$app" ]]; then
        ln -s "$source" "$app"
    fi
    echo "UTM installed at $app"

utm: utm-install
    open "$HOME/Applications/UTM.app"

# Microsoft generates expiring download links after browser validation.
windows-iso:
    open "https://www.microsoft.com/en-us/software-download/windows11arm64"

# Use the SHA256 for the selected language from Microsoft's download page.
windows-iso-verify path sha256:
    printf '%s  %s\n' {{quote(sha256)}} {{quote(path)}} | shasum -a 256 --check -

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

unit-check suite="all":
    #!/usr/bin/env bash
    set -euo pipefail
    suite={{quote(suite)}}
    case "$suite" in all|rust|python|wasm) ;; *) echo 'unit-check suite must be all, rust, python or wasm' >&2; exit 2 ;; esac
    if [[ "$suite" == all || "$suite" == rust ]]; then
        nix shell --impure .#game --command cargo test --manifest-path simulation/Cargo.toml --locked --lib
    fi
    if [[ "$suite" == all || "$suite" == python ]]; then
        just python-test
    fi
    if [[ "$suite" == wasm ]]; then
        devenv shell uv run --locked --extra lab pytest tests/test_flight_engine.py
    fi

# Python recipes always inspect the whole project, without file filters.
python-test:
    devenv shell uv run --locked --extra lab pytest

python-format:
    devenv shell uv run --locked ruff format .

python-check:
    devenv tasks run wsorb:lint

# Compile and lint the actual engine and bundle the thin browser launcher.
check:
    devenv tasks run wsorb:lint
    nix shell --impure .#rust --command cargo fmt --manifest-path simulation/Cargo.toml -- --check
    nix shell --impure .#game --command cargo clippy --manifest-path simulation/Cargo.toml --locked --target wasm32-unknown-unknown --lib -- -D warnings
    nix shell --impure .#game --command npm --prefix worker run build:web
    git diff --check

# Complete repository validation. Run every phase and retain any failure.
verify:
    #!/usr/bin/env bash
    set -uo pipefail
    result=0
    just build || result=1
    just check || result=1
    WSORB_CHECKPOINT_INTEGRATION=1 WSORB_DIAGNOSTICS_INTEGRATION=1 PYTEST_ADDOPTS="${PYTEST_ADDOPTS:-} -ra" just unit-check all || result=1
    just platform-check || result=1
    exit "$result"

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

local-check target="build":
    #!/usr/bin/env bash
    set -euo pipefail
    nix shell --impure .#game --command node --input-type=module - {{quote(target)}} <<'JS'
    import {spawn} from 'node:child_process';
    import {readFile} from 'node:fs/promises';
    if (['live','load'].includes(process.argv[2])) {
      const c=JSON.parse(await readFile(process.env.WSORB_LOCAL_CONNECTION||'.devenv/wsorb-local.json','utf8'));
      const origin=new URL(c.url);
      if(origin.protocol!=='http:'||!['127.0.0.1','localhost','[::1]'].includes(origin.hostname)||origin.username||origin.password||origin.pathname!=='/'||origin.search||origin.hash)throw new Error('Local diagnostics require the configured loopback origin');
      if(process.argv[2]==='load'){
        const {loadVerifiedEngine}=await import('./worker/web/engine-loader.ts');
        const response=await fetch(new URL('/sim/manifest.json',origin),{redirect:'error',signal:AbortSignal.timeout(5000)});
        if(!response.ok)throw new Error('Engine manifest is unavailable');
        const manifest=await response.json();
        const result=await loadVerifiedEngine(new URL(`/sim/wsorb_bevy_bg.wasm?v=${manifest.sha256}`,origin).href,manifest);
        console.log(JSON.stringify({runtime:process.version,measurement:'real HTTP and WebAssembly compilation, not browser/GPU rendering',sha256:manifest.sha256,...result.timings}));
      } else {
      const headers={Authorization:`Bearer ${c.token}`};
      const statusResponse=await fetch(new URL('/__local/status',origin),{headers,redirect:'error',signal:AbortSignal.timeout(5000)});
      const status=await statusResponse.json();
      if(!statusResponse.ok||status.enabled!==true||typeof status.connected!=='boolean'||typeof status.busy!=='boolean')throw new Error('Authenticated local diagnostics are unavailable');
      console.log(JSON.stringify({url:c.url,connected:status.connected,busy:status.busy,engine_build:status.engine_build}));
      if(status.connected&&!status.busy){
        const response=await fetch(new URL('/__local/call',origin),{method:'POST',headers:{...headers,'Content-Type':'application/json'},body:JSON.stringify({method:'snapshot'}),redirect:'error',signal:AbortSignal.timeout(8000)});
        const result=await response.json();
        if(!response.ok||result.ok!==true)throw new Error('Live snapshot unavailable; no flight command was sent');
        const s=result.value;
        console.log(JSON.stringify({time_s:s.observation.time_s,running:s.runtime.running,mission:s.observation.mission.status,renderer:s.renderer}));
      }
      }
    } else if (process.argv[2] === 'build') {
    const child=spawn(process.execPath,['worker/scripts/local.mjs'],{env:{...process.env,PORT:'0'},stdio:['ignore','pipe','inherit']});
    try {
      const url=await new Promise((resolve,reject)=>{child.once('error',reject);child.stdout.on('data',b=>{const match=String(b).match(/http:\/\/127\.0\.0\.1:\d+\//);if(match)resolve(match[0]);});setTimeout(()=>reject(new Error('Local startup timed out')),5000).unref();});
      const page=await fetch(url), manifest=await (await fetch(new URL('sim/manifest.json',url))).json();
      const wasm=await fetch(new URL('sim/wsorb_bevy_bg.wasm',url),{method:'HEAD'});
      const denied=await fetch(url,{method:'POST'});
      if(page.status!==200||wasm.status!==200||Number(wasm.headers.get('content-length'))!==manifest.bytes||denied.status!==405||page.headers.get('x-content-type-options')!=='nosniff')throw new Error('Local HTTP contract failed');
      console.log(JSON.stringify({url,page:page.status,wasm:wasm.status,bytes:manifest.bytes,writeRejected:denied.status,result:'PASS'}));
    } finally {child.kill('SIGTERM');}
    } else throw new Error('local-check target must be build, live or load');
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
    const web_assets=[];
    for(const name of ['index.html','styles.css','app.js']) {
      const asset=await request(name==='index.html'?'':name);
      assert.equal(asset.status,200);
      const actual=Buffer.from(await asset.arrayBuffer());
      const local=await readFile(`worker/public/${name}`);
      const digest=value=>createHash('sha256').update(value).digest('hex');
      assert.equal(digest(actual),digest(local),`Hosted ${name} differs from the approved local build`);
      web_assets.push({name,bytes:actual.length,sha256:digest(actual)});
    }
    console.log(JSON.stringify({url:base,bytes:bytes.length,sha256,web_assets,result:'PASS'},null,2));
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

# Release browser fixes with an explicitly approved, already-built engine.
# This does not compile unrelated in-progress native changes.
deploy-web engine_sha message:
    nix shell --impure .#game --command node --input-type=module -e 'import{readFileSync}from"node:fs";import{createHash}from"node:crypto";const m=JSON.parse(readFileSync("worker/public/sim/manifest.json"));const b=readFileSync("worker/public/sim/wsorb_bevy_bg.wasm");const sha=createHash("sha256").update(b).digest("hex");if(sha!==process.argv[1]||m.sha256!==sha||m.bytes!==b.length)throw Error("Prebuilt engine does not match the approved release");console.log(`Approved engine: ${sha}`);' {{quote(engine_sha)}}
    just account
    just web-build
    just deploy-dry-run
    nix shell --impure .#game --command npm --prefix worker run wrangler -- deploy --message {{quote(message)}}

# Per-process fallback for intermittent dual-stack connection failures.
deploy-ipv4:
    NODE_OPTIONS="${NODE_OPTIONS:-} --dns-result-order=ipv4first" nix shell --impure .#game --command npm --prefix worker run deploy

deploy-debug:
    WSO_CF_NETWORK_DIAGNOSTICS=1 nix shell --impure .#game --command npm --prefix worker run deploy

deploy-http2:
    WSO_CF_HTTP2=1 WSO_CF_NETWORK_DIAGNOSTICS=1 nix shell --impure .#game --command npm --prefix worker run deploy

tasks:
    bd list --status open --json --sandbox

controllers +args:
    devenv shell uv run --locked wsorb controllers {{args}}

# Publish only curated own-ship measurements from a completed matched sweep.
scoreboard input output:
    devenv shell uv run --locked wsorb scoreboard --input {{quote(input)}} --output {{quote(output)}}

# Correct a curated publication only if its existing generated digests still match.
scoreboard-refresh input output:
    devenv shell uv run --locked wsorb scoreboard --input {{quote(input)}} --output {{quote(output)}} --refresh

# Default sweep has 50 trials. Reduce seeds/counts for a quick workflow check.
flight-sweep config="examples/flight_sweep.json" output="":
    devenv shell uv run --locked --extra lab python examples/flight_sweep.py --config {{quote(config)}} --output {{quote(output)}}

flight-analyze output:
    devenv shell uv run --locked --extra lab python examples/flight_sweep.py --analyze-only --output {{quote(output)}}

flight-notebook:
    devenv shell uv run --locked --extra lab marimo edit notebooks/20261029-HEADLESS-EXPERIMENTS/usage.py

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

# Allow the licensed source maps without changing repository or global JJ config.
status:
    jj --config snapshot.max-new-file-size=6291456 status

review:
    git diff --check
    jj diff --stat

diff +paths:
    jj diff --git {{paths}}

history:
    jj log -r 'ancestors(@, 7)' --no-pager

describe revision message:
    jj describe -r {{quote(revision)}} -m {{quote(message)}}

fetch:
    jj git fetch --remote origin

pull-requests:
    gh pr list --state open --limit 100 --json number,title,headRefName,baseRefName,url

merge message left right:
    jj new -r {{quote(left)}} -r {{quote(right)}} -m {{quote(message)}}

merge-history message left right:
    jj new --no-edit -r {{quote(left)}} -r {{quote(right)}} -m {{quote(message)}}

merge-resolve-from revision +paths:
    jj restore --from {{quote(revision)}} -- {{paths}}

merge-resolve-into revision source +paths:
    jj restore --from {{quote(source)}} --into {{quote(revision)}} -- {{paths}}

rebase-work revision:
    jj rebase --source @ --onto {{quote(revision)}}

tree-diff left right:
    jj diff --from {{quote(left)}} --to {{quote(right)}} --summary

commit message +paths:
    jj commit -m {{quote(message)}} {{paths}}

push revision="@-":
    jj bookmark set main -r {{quote(revision)}}
    jj git push --bookmark main
