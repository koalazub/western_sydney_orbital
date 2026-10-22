{
  description = "Western Sydney Orbital development shell";

  inputs = {
    # Keep Intel macOS supported; nixpkgs rolling dropped x86_64-darwin.
    nixpkgs.url = "github:NixOS/nixpkgs/nixpkgs-26.05-darwin";
  };

  outputs =
    { nixpkgs, ... }:
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
            environment = import ./devenv.nix { inherit pkgs; };
          in
          pkgs.mkShell (
            {
              name = "western-sydney-orbital";
              packages = environment.packages ++ [ pkgs.devenv ];
              shellHook = environment.enterShell or "";
            }
            // environment.env
          );
      });
      formatter = forAllSystems (system: nixpkgs.legacyPackages.${system}.nixfmt-rfc-style);
    };
}
