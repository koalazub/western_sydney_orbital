{ pkgs }:
let
  version = "0.2.129";
  system = pkgs.stdenv.hostPlatform.system;
  platform =
    {
      "aarch64-darwin" = {
        target = "aarch64-apple-darwin";
        sha256 = "81d4a23d56b3c3eb8187658329116d50e0b228a93b343825fb71f70179051cd1";
      };
      "x86_64-darwin" = {
        target = "x86_64-apple-darwin";
        sha256 = "7e028879a68ec53dae14048b55cc5215f6df75b17a9c1426fac8fa086d3202fd";
      };
      "aarch64-linux" = {
        target = "aarch64-unknown-linux-musl";
        sha256 = "2ed4351c35dd9440308bbb02767d47ea278efe851a52465300f3c94f5b6c2a87";
      };
      "x86_64-linux" = {
        target = "x86_64-unknown-linux-musl";
        sha256 = "82d12bb940e2d4e72e0d5605387fc1b8ca179044e012b620f0ce4e7440e8320e";
      };
    }
    .${system} or (throw "wasm-bindgen-cli ${version} is unsupported on ${system}");
in
pkgs.stdenvNoCC.mkDerivation {
  pname = "wasm-bindgen-cli";
  inherit version;

  src = pkgs.fetchurl {
    url = "https://github.com/wasm-bindgen/wasm-bindgen/releases/download/${version}/wasm-bindgen-${version}-${platform.target}.tar.gz";
    inherit (platform) sha256;
  };

  dontConfigure = true;
  dontBuild = true;

  installPhase = ''
    runHook preInstall
    mkdir -p "$out/bin"
    binary="$(find . -type f -name wasm-bindgen -print -quit)"
    test -n "$binary"
    install -m755 "$binary" "$out/bin/wasm-bindgen"
    runHook postInstall
  '';

  meta = {
    description = "CLI tool for the wasm-bindgen project";
    homepage = "https://github.com/wasm-bindgen/wasm-bindgen";
    license = [
      pkgs.lib.licenses.mit
      pkgs.lib.licenses.asl20
    ];
    mainProgram = "wasm-bindgen";
    platforms = builtins.attrNames {
      aarch64-darwin = null;
      x86_64-darwin = null;
      aarch64-linux = null;
      x86_64-linux = null;
    };
  };
}
