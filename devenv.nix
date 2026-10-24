{ pkgs, ... }:
let
  nodejs = import ./nix/nodejs.nix { inherit pkgs; };
  wasmBindgen = import ./nix/wasm-bindgen.nix { inherit pkgs; };
  workerCli =
    name: package:
    pkgs.writeShellScriptBin name ''
      export PATH="${nodejs}/bin:$PATH"

      root="''${DEVENV_ROOT:-}"
      if [ -z "$root" ]; then
        root="$(git rev-parse --show-toplevel 2>/dev/null || true)"
      fi

      if [ -z "$root" ] || [ ! -x "$root/worker/node_modules/.bin/${package}" ]; then
        printf '%s\n' "${name} is installed with the Worker dependencies. Run: npm ci --prefix <root>/worker" >&2
        exit 1
      fi

      exec node "$root/worker/scripts/cloudflare.mjs" "${package}" "$@"
    '';
in
{
  languages.rust = {
    enable = true;
    channel = "nightly";
    targets = [ "wasm32-unknown-unknown" ];
  };

  # Rust builds target WASM here; the C module's default native debugger is
  # unnecessary and would pull LLDB into the development shell on macOS.
  languages.c.debugger = null;

  packages = with pkgs; [
    uv
    python313
    git
    gh
    jujutsu
    gnumake
    just
    jq
    direnv
    nodejs
    wasmBindgen
    binaryen
    pkg-config
    libiconv
    (workerCli "cf" "cf")
    (workerCli "wrangler" "wrangler")
  ];

  env = {
    UV_PYTHON = "${pkgs.python313}/bin/python3";
    UV_PYTHON_DOWNLOADS = "never";
    UV_PROJECT_ENVIRONMENT = ".venv";
    LD_LIBRARY_PATH = pkgs.lib.optionalString pkgs.stdenv.isLinux (
      pkgs.lib.makeLibraryPath [ pkgs.stdenv.cc.cc.lib ]
    );
  };

  tasks = {
    "wsorb:sync" = {
      exec = "uv sync --locked --extra lab";
    };
    "wsorb:test" = {
      exec = "uv run --locked --extra lab pytest";
    };
    "wsorb:lint" = {
      exec = "uv run --locked --extra lab ruff check . && uv run --locked --extra lab ruff format --check . && uv run --locked --extra lab marimo check --strict notebooks";
    };
  };

  processes = {
    notebook.exec = "WSORB_PORT=\"\${WSORB_PORT:-8765}\" bash scripts/serve-tailnet";
    simulation.exec = "exec env WSORB_LAB=1 PORT=\"\${WSORB_GAME_PORT:-61829}\" node worker/scripts/local.mjs";
    lab.exec = "exec uv run --locked --extra lab marimo edit notebooks/flight_lab.py --host 127.0.0.1 --port \"\${WSORB_LAB_PORT:-2718}\" --headless --no-sandbox --skip-update-check";
  };
}
