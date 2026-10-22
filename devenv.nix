{ pkgs, ... }:
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
