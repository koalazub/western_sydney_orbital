{
  description = "Western Sydney Orbital development shell";

  inputs = {
    # Keep Intel macOS supported; nixpkgs rolling dropped x86_64-darwin.
    nixpkgs.url = "github:NixOS/nixpkgs/36fd15bbb657815e2028e31d715c2e43a1230c91";
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
              packages = environment.packages;
              shellHook = environment.enterShell or "";
            }
            // environment.env
          );
      });
      formatter = forAllSystems (system: nixpkgs.legacyPackages.${system}.nixfmt-rfc-style);
    };
}
