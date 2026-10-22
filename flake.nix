{
  description = "Western Sydney Orbital development toolkit";

  inputs = {
    # Keep Intel macOS supported; nixpkgs rolling dropped x86_64-darwin.
    nixpkgs.url = "github:NixOS/nixpkgs/36fd15bbb657815e2028e31d715c2e43a1230c91";
    fenix = {
      url = "github:nix-community/fenix/02d6357e9d59c4626a82b27371ae6e90508839e5";
      inputs.nixpkgs.follows = "nixpkgs";
    };
  };

  outputs =
    { nixpkgs, fenix, ... }:
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
            environment = import ./devenv.nix { inherit pkgs; inputs = { inherit fenix; }; };
          in
          pkgs.mkShell (
            {
              name = "western-sydney-orbital";
              packages = environment.packages;
              shellHook = environment.enterShell or "";
            }
            // environment.env
          );
      });
      formatter = forAllSystems (system: nixpkgs.legacyPackages.${system}.nixfmt-rfc-style);
    };
}
