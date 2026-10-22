{ pkgs }:
let
  system = pkgs.stdenv.hostPlatform.system;
  platform = {
    "aarch64-darwin" = {
      archive = "darwin-arm64";
      sha256 = "18830bf448fe187c650dc28c98aa8c968ed1d40b6dd032c081e70a384a1bf660";
    };
    "x86_64-darwin" = {
      archive = "darwin-x64";
      sha256 = "69316aaafb4dfcb25ac7b9a460f5c60928a37580284fde410144d0e6c8672d0f";
    };
    "aarch64-linux" = {
      archive = "linux-arm64";
      sha256 = "42aa04922591ee5d4dbc7740d452621acf28508239daa512f5ee8c47ee224fdb";
    };
    "x86_64-linux" = {
      archive = "linux-x64";
      sha256 = "6170b2d474e1c626f43cff3adc118df9fe3b573258a4fc3d850c8dbc6b079bbd";
    };
  }.${system} or (throw "Node.js 26.12.1 is unsupported on ${system}");
  isLinux = pkgs.stdenv.hostPlatform.isLinux;
in
pkgs.stdenvNoCC.mkDerivation {
  pname = "nodejs";
  version = "26.12.1";

  src = pkgs.fetchurl {
    url = "https://nodejs.org/dist/v26.12.1/node-v26.12.1-${platform.archive}.tar.xz";
    inherit (platform) sha256;
  };

  dontConfigure = true;
  dontBuild = true;

  nativeBuildInputs = pkgs.lib.optionals isLinux [ pkgs.autoPatchelfHook ];
  buildInputs = pkgs.lib.optionals isLinux [ pkgs.stdenv.cc.cc.lib ];

  installPhase = ''
    runHook preInstall
    mkdir -p "$out"
    cp -a . "$out/"
    runHook postInstall
  '';

  meta = {
    description = "Node.js JavaScript runtime";
    homepage = "https://nodejs.org/";
    license = pkgs.lib.licenses.mit;
    mainProgram = "node";
    platforms = builtins.attrNames {
      aarch64-darwin = null;
      x86_64-darwin = null;
      aarch64-linux = null;
      x86_64-linux = null;
    };
  };
}
