{
  description = "Western Sydney Orbital development toolkit";

  inputs = {
    # Keep Intel macOS supported; nixpkgs rolling dropped x86_64-darwin.
    nixpkgs.url = "github:NixOS/nixpkgs/36fd15bbb657815e2028e31d715c2e43a1230c91";
    rust-overlay = {
      url = "github:oxalica/rust-overlay";
      inputs.nixpkgs.follows = "nixpkgs";
    };
  };

  outputs =
    { nixpkgs, rust-overlay, ... }:
    let
      systems = [
        "x86_64-linux"
        "aarch64-linux"
        "x86_64-darwin"
        "aarch64-darwin"
      ];
      forAllSystems = nixpkgs.lib.genAttrs systems;
      perSystem =
        system:
        let
          pkgs = import nixpkgs {
            inherit system;
            overlays = [ rust-overlay.overlays.default ];
          };
          nodejs = import ./nix/nodejs.nix { inherit pkgs; };
          wasmBindgen = import ./nix/wasm-bindgen.nix { inherit pkgs; };
          rust = pkgs.rust-bin.nightly.latest.default.override {
            targets = [ "wasm32-unknown-unknown" ];
          };
          # Keep wrangler.jsonc JSON-compatible so both CLIs share its account declaration.
          workerConfig = builtins.fromJSON (builtins.readFile ./worker/wrangler.jsonc);
          linuxTools = pkgs.lib.optionals pkgs.stdenv.isLinux [ pkgs.stdenv.cc ];
          wrappers = {
            game = pkgs.writeShellApplication {
              name = "wsorb-game";
              runtimeInputs = [ nodejs ];
              text = ''
                export WSORB_LAB=1
                export PORT="''${WSORB_GAME_PORT:-61829}"
                exec node worker/scripts/local.mjs
              '';
            };
            lab = pkgs.writeShellApplication {
              name = "wsorb-lab";
              runtimeInputs = [
                pkgs.uv
                pkgs.python313
              ];
              text = ''
                export UV_PYTHON="${pkgs.python313}/bin/python3"
                export UV_PROJECT_ENVIRONMENT=.venv
                exec uv run --locked --extra lab wsorb lab
              '';
            };
            share = pkgs.writeShellApplication {
              name = "wsorb-share";
              runtimeInputs = [
                pkgs.uv
                pkgs.python313
                pkgs.bash
              ];
              text = ''
                export UV_PYTHON="${pkgs.python313}/bin/python3"
                export UV_PYTHON_DOWNLOADS=never
                export UV_PROJECT_ENVIRONMENT=.venv
                exec bash scripts/serve-tailnet
              '';
            };
          };
        in
        {
          devShells.default = pkgs.mkShell {
            packages = [
              pkgs.uv
              pkgs.python313
              pkgs.git
              pkgs.gh
              pkgs.jujutsu
              pkgs.gnumake
              pkgs.just
              pkgs.jq
              pkgs.direnv
              nodejs
              wasmBindgen
              pkgs.binaryen
              pkgs.pkg-config
              pkgs.libiconv
              rust
            ]
            ++ linuxTools;
            env = {
              CLOUDFLARE_ACCOUNT_ID = workerConfig.account_id;
              UV_PYTHON = "${pkgs.python313}/bin/python3";
              UV_PYTHON_DOWNLOADS = "never";
              UV_PROJECT_ENVIRONMENT = ".venv";
              LD_LIBRARY_PATH = pkgs.lib.optionalString pkgs.stdenv.isLinux (
                pkgs.lib.makeLibraryPath [ pkgs.stdenv.cc.cc.lib ]
              );
            };
            shellHook = ''
              export PATH="$PWD/worker/node_modules/.bin:$PATH"
            '';
          };
          packages = {
            rust = rust;
            build-tools = pkgs.buildEnv {
              name = "phobos-build-tools";
              paths = [
                rust
                nodejs
                wasmBindgen
                pkgs.binaryen
              ]
              ++ linuxTools;
            };
            inherit (wrappers) game lab share;
          }
          // pkgs.lib.optionalAttrs pkgs.stdenv.isDarwin {
            # Windows DirectX support needs UTM 5; retain nixpkgs' Intel macOS support.
            utm = pkgs.utm.overrideAttrs (finalAttrs: {
              version = "5.0.8";
              src = pkgs.fetchurl {
                url = "https://github.com/utmapp/UTM/releases/download/v${finalAttrs.version}/UTM.dmg";
                sha256 = "7b2d7b2e2a02b3de1757ef19a8f36573e6d9cceae8c8cca0abcbcaa4a852019a";
              };
            });
          };
          formatter = pkgs.nixfmt-tree;
          apps = {
            game = {
              type = "app";
              program = "${wrappers.game}/bin/wsorb-game";
            };
            lab = {
              type = "app";
              program = "${wrappers.lab}/bin/wsorb-lab";
            };
            share = {
              type = "app";
              program = "${wrappers.share}/bin/wsorb-share";
            };
          };
        };
    in
    {
      devShells = forAllSystems (system: {
        default = (perSystem system).devShells.default;
      });
      packages = forAllSystems (system: (perSystem system).packages);
      apps = forAllSystems (system: (perSystem system).apps);
      formatter = forAllSystems (system: (perSystem system).formatter);
    };
}
