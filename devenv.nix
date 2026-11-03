{ pkgs, ... }:
let
  nodejs = import ./nix/nodejs.nix { inherit pkgs; };
  wasmBindgen = import ./nix/wasm-bindgen.nix { inherit pkgs; };
  # Keep wrangler.jsonc JSON-compatible so both CLIs share its account declaration.
  workerConfig = builtins.fromJSON (builtins.readFile ./worker/wrangler.jsonc);
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
  ];

  env = {
    CLOUDFLARE_ACCOUNT_ID = workerConfig.account_id;
    UV_PYTHON = "${pkgs.python313}/bin/python3";
    UV_PYTHON_DOWNLOADS = "never";
    UV_PROJECT_ENVIRONMENT = ".venv";
    LD_LIBRARY_PATH = pkgs.lib.optionalString pkgs.stdenv.isLinux (
      pkgs.lib.makeLibraryPath [ pkgs.stdenv.cc.cc.lib ]
    );
  };

  enterShell = ''
    export PATH="$DEVENV_ROOT/worker/node_modules/.bin:$PATH"
  '';

  processes = {
    notebook.exec = "WSORB_PORT=\"\${WSORB_PORT:-8765}\" bash scripts/serve-tailnet";
    simulation.exec = "exec env WSORB_LAB=1 PORT=\"\${WSORB_GAME_PORT:-61829}\" node worker/scripts/local.mjs";
    lab.exec = "exec uv run --locked --extra lab wsorb lab";
  };
}
