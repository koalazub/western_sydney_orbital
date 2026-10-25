"""Prepare checked-in mission imagery. This script never accesses the network.

Run explicitly with the existing environment:
devenv shell uv run --locked --extra lab worker/scripts/prepare-planetary-assets.py
Ordinary browser builds only copy the resulting PNGs.
"""

import hashlib
import json
from pathlib import Path

import numpy as np
from PIL import Image, ImageChops

ASSETS = Path(__file__).resolve().parents[1] / "web" / "assets"
SOURCE = ASSETS / "source"
OUTPUT = ASSETS / "textures"
MAX_BYTES = 25 * 1024 * 1024
FACE_SIZE = 512

SOURCES = {
    "phobos-viking-dlrcontrol-7200.jpg": {
        "url": "https://upload.wikimedia.org/wikipedia/commons/a/a9/"
        "Phobos_Viking_Mosaic_DLRcontrol_7200.jpg",
        "page": "https://astrogeology.usgs.gov/search/map/phobos_viking_global_mosaic_5m",
        "credit": "Phil Stooke / NASA Planetary Data System / USGS",
    },
    "mars-viking-nasa.jpg": {
        "url": "https://assets.science.nasa.gov/content/dam/science/cds/3d/resources/image/"
        "mars/Mars.jpg",
        "page": "https://science.nasa.gov/3d-resources/mars/",
        "credit": "NASA / Jet Propulsion Laboratory & Caltech; Viking imagery processed by USGS",
    },
    "starmap-nasa-svs-4k.jpg": {
        "url": "https://svs.gsfc.nasa.gov/vis/a000000/a003800/a003895/starmap_4k.jpg",
        "page": "https://svs.gsfc.nasa.gov/3895/",
        "credit": "NASA/Goddard Space Flight Center Scientific Visualization Studio; "
        "Ernie Wright and Tom Bridgman",
    },
}


def record(path: Path) -> dict:
    data = path.read_bytes()
    if len(data) >= MAX_BYTES:
        raise ValueError(f"Asset exceeds the 25 MiB limit: {path}")
    with Image.open(path) as image:
        image.load()
        width, height = image.size
        mode = image.mode
    return {
        "path": path.relative_to(ASSETS).as_posix(),
        "bytes": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
        "width": width,
        "height": height,
        "mode": mode,
    }


def surface_map(name: str, output: str, width: int) -> None:
    with Image.open(SOURCE / name) as source:
        source.load()
        if source.width != 2 * source.height:
            raise ValueError(f"Expected a 2:1 global map: {name}")
        # Both maps are north-up, centered on zero longitude. Our mesh grid
        # starts at zero longitude, so move that meridian to the left edge.
        image = source.convert("RGB").resize((width, width // 2), Image.Resampling.LANCZOS)
        image = ImageChops.offset(image, width // 2, 0)
        image.save(OUTPUT / output, optimize=True)


def sample_bilinear(pixels: np.ndarray, u: np.ndarray, v: np.ndarray) -> np.ndarray:
    """Sample an existing equirectangular map, wrapping longitude only."""
    height, width, _ = pixels.shape
    x = (u * width - 0.5) % width
    y = np.clip(v * height - 0.5, 0, height - 1)
    x0, y0 = np.floor(x).astype(int), np.floor(y).astype(int)
    x1, y1 = (x0 + 1) % width, np.minimum(y0 + 1, height - 1)
    tx, ty = (x - x0)[..., None], (y - y0)[..., None]
    top = pixels[y0, x0] * (1 - tx) + pixels[y0, x1] * tx
    bottom = pixels[y1, x0] * (1 - tx) + pixels[y1, x1] * tx
    return np.clip(np.rint(top * (1 - ty) + bottom * ty), 0, 255).astype(np.uint8)


def star_cubemap() -> None:
    with Image.open(SOURCE / "starmap-nasa-svs-4k.jpg") as source:
        pixels = np.asarray(source.convert("RGB"), dtype=np.float64)
    if pixels.shape[:2] != (2048, 4096):
        raise ValueError("Expected the original NASA SVS 4096 x 2048 star map")
    coordinates = 2 * (np.arange(FACE_SIZE, dtype=float) + 0.5) / FACE_SIZE - 1
    s, t = np.meshgrid(coordinates, coordinates)
    one = np.ones_like(s)
    # WebGPU cube face order and orientation: +X, -X, +Y, -Y, +Z, -Z.
    directions = [
        (one, -t, -s),
        (-one, -t, s),
        (s, one, t),
        (s, -one, -t),
        (s, -t, one),
        (-s, -t, -one),
    ]
    faces = []
    for x, y, z in directions:
        norm = np.sqrt(x * x + y * y + z * z)
        # NASA's celestial map has RA=0 at its center, increasing to the left.
        u = (0.5 - np.arctan2(z, x) / (2 * np.pi)) % 1
        v = 0.5 - np.arcsin(np.clip(y / norm, -1, 1)) / np.pi
        faces.append(sample_bilinear(pixels, u, v))
    # No invented stars, constellations, gradients, noise, or added brightness.
    Image.fromarray(np.concatenate(faces, axis=0)).save(
        OUTPUT / "stars-nasa-svs-cube.png", optimize=True
    )


def main() -> None:
    OUTPUT.mkdir(parents=True, exist_ok=True)
    sources = [record(SOURCE / name) | info for name, info in SOURCES.items()]
    surface_map("phobos-viking-dlrcontrol-7200.jpg", "phobos-viking-albedo.png", 2048)
    # NASA supplies a 1440 x 720 map. Keep its real resolution, do not upscale.
    with Image.open(SOURCE / "mars-viking-nasa.jpg") as mars:
        mars_width = min(2048, mars.width)
    surface_map("mars-viking-nasa.jpg", "mars-viking-albedo.png", mars_width)
    star_cubemap()
    textures = [record(path) for path in sorted(OUTPUT.glob("*.png"))]
    manifest = {
        "schema": 1,
        "license_notice": "ATTRIBUTION.md",
        "sources": sources,
        "textures": textures,
        "cube_face_order": ["+X", "-X", "+Y", "-Y", "+Z", "-Z"],
        "cube_face_size": FACE_SIZE,
        "surface_coordinates": "north-up; longitude 0..360 degrees; +Y north, +X zero, +Z east",
    }
    (ASSETS / "asset-manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print(json.dumps(manifest, indent=2))


if __name__ == "__main__":
    main()
