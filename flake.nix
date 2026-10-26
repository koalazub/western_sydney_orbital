{
  description = "Western Sydney Orbital development toolkit";

  inputs = {
    # Keep Intel macOS supported; nixpkgs rolling dropped x86_64-darwin.
    nixpkgs.url = "github:NixOS/nixpkgs/36fd15bbb657815e2028e31d715c2e43a1230c91";
    rust-overlay = {
      url = "github:oxalica/rust-overlay";
      inputs.nixpkgs.follows = "nixpkgs";
    };
    devenv = {
      url = "github:cachix/devenv";
      inputs.nixpkgs.follows = "nixpkgs";
    };
  };

  outputs =
    { nixpkgs, devenv, ... }@inputs:
    let
      systems = [
        "x86_64-linux"
        "aarch64-linux"
        "x86_64-darwin"
        "aarch64-darwin"
      ];
      forAllSystems = nixpkgs.lib.genAttrs systems;
    in
    {
      devShells = forAllSystems (system: {
        default =
          let
            pkgs = nixpkgs.legacyPackages.${system};
          in
          devenv.lib.mkShell {
            inherit inputs pkgs;
            modules = [ ./devenv.nix ];
          };
      });
      packages = forAllSystems (
        system:
        let
          pkgs = nixpkgs.legacyPackages.${system};
        in
        {
          rust = inputs.self.devShells.${system}.default.config.languages.rust.toolchainPackage;
          game = pkgs.buildEnv {
            name = "phobos-build-tools";
            paths = [
              inputs.self.packages.${system}.rust
              (import ./nix/nodejs.nix { inherit pkgs; })
              (import ./nix/wasm-bindgen.nix { inherit pkgs; })
              pkgs.binaryen
            ]
            ++ pkgs.lib.optionals pkgs.stdenv.isLinux [ pkgs.stdenv.cc ];
          };
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
        }
      );
      formatter = forAllSystems (system: nixpkgs.legacyPackages.${system}.nixfmt-tree);
    };
}
