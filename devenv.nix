{ pkgs, inputs, ... }:
let
  system = pkgs.stdenv.hostPlatform.system;
  nodejs = import ./nix/nodejs.nix { inherit pkgs; };
  rustToolchain = inputs.fenix.packages.${system}.combine [
    inputs.fenix.packages.${system}.stable.rustc
    inputs.fenix.packages.${system}.stable.cargo
    inputs.fenix.packages.${system}.stable.rustfmt
    inputs.fenix.packages.${system}.targets.wasm32-unknown-unknown.stable.rust-std
  ];
  workerCli = name: package: pkgs.writeShellScriptBin name ''
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
  packages = with pkgs; [
    uv
    python313
    git
    gh
    jujutsu
    gnumake
    jq
    direnv
    nodejs
    rustToolchain
    wasm-bindgen-cli
    pkg-config
    libiconv
    (workerCli "cf" "cf")
    (workerCli "wrangler" "wrangler")
  ];

  env = {
    UV_PYTHON = "${pkgs.python313}/bin/python3";
    UV_PYTHON_DOWNLOADS = "never";
    UV_PROJECT_ENVIRONMENT = ".venv";
    LD_LIBRARY_PATH = pkgs.lib.optionalString pkgs.stdenv.isLinux
      (pkgs.lib.makeLibraryPath [ pkgs.stdenv.cc.cc.lib ]);
  };

  tasks = {
    "wsorb:sync" = { exec = "uv sync --locked"; };
    "wsorb:test" = { exec = "uv run --locked pytest"; };
    "wsorb:lint" = { exec = "uv run --locked ruff check . && uv run --locked ruff format --check ."; };
  };

  processes = {
    notebook.exec = "WSORB_PORT=\"\${WSORB_PORT:-8765}\" bash scripts/serve-tailnet";
  };
}
