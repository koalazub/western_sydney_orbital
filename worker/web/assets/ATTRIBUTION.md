# Planetary imagery and star-map notices

These are redistributed source images and locally resampled texture maps, not
generated terrain, noise textures, or captured simulation frames. No agency or
author listed here endorses this simulator.

## Phobos

Credit: Phil Stooke / NASA Planetary Data System / USGS Astrogeology Science Center.
Stooke, P. 2012, *Stooke Small Bodies Maps V2.0*,
MULTI-SA-MULTI-6-STOOKEMAPS-V2.0. Viking imagery, reprojected to DLR control.
Original Cornell mosaic: Simonelli, Thomas, Carcich and Veverka, 1993,
<https://doi.org/10.1006/icar.1993.1057>.

USGS identifies this product as public domain and requests author citation:
<https://astrogeology.usgs.gov/search/map/phobos_viking_global_mosaic_5m>.
The checked-in 7200 x 3600 JPEG is the public-domain copy at
<https://commons.wikimedia.org/wiki/File:Phobos_Viking_Mosaic_DLRcontrol_7200.jpg>.
Download:
<https://upload.wikimedia.org/wikipedia/commons/a/a9/Phobos_Viking_Mosaic_DLRcontrol_7200.jpg>.

`textures/phobos-viking-albedo.png` is a 2048 x 1024 RGB Lanczos reduction.
Longitude was shifted by half the image width to match the existing physical
mesh's zero-longitude seam. Brightness and contrast were not synthesized.
This is a grayscale spacecraft mosaic used for relative surface color/detail,
not a calibrated, illumination-free albedo or height map. The source includes
lighting seams and editorial blending described by USGS. No normal map was
inferred from its brightness.

## Mars

Credit: NASA / Jet Propulsion Laboratory & Caltech. Viking images processed at
USGS, from the JPL/Caltech planetary-map database.
Source page: <https://science.nasa.gov/3d-resources/mars/>.
Download:
<https://assets.science.nasa.gov/content/dam/science/cds/3d/resources/image/mars/Mars.jpg>.

`textures/mars-viking-albedo.png` preserves the supplied map's RGB colors and
native 1440 x 720 resolution. It is shifted by half its width to match
the mesh's zero-longitude seam. This is photographic base-color imagery, not
an elevation model or a calibrated bidirectional-reflectance measurement.

NASA's media guidelines permit factual educational/informational simulations
and redistribution, with source acknowledgement and no implied endorsement:
<https://www.nasa.gov/nasa-brand-center/images-and-media/>.
NASA 3D content is generally not subject to US copyright. The source page has
no separate third-party copyright restriction for this texture. Agency names
are credits, not permission to use NASA's protected logos or insignia.

## Stars

Credit: NASA/Goddard Space Flight Center Scientific Visualization Studio.
Ernie Wright and Tom Bridgman, *Deep Star Maps*, 2012. Stellar positions,
brightnesses and colors derive from the Bright Star, Tycho-2 and UCAC3 catalogs.
Source page: <https://svs.gsfc.nasa.gov/3895/>.
Download:
<https://svs.gsfc.nasa.gov/vis/a000000/a003800/a003895/starmap_4k.jpg>.
SVS states that this content is public domain and may be used and redistributed:
<https://svs.gsfc.nasa.gov/help/>.

`textures/stars-nasa-svs-cube.png` resamples the actual 4096 x 2048 celestial map
into six 512 x 512 faces stacked vertically in WebGPU order +X, -X, +Y, -Y, +Z,
-Z. Bilinear sampling wraps right ascension at the map seam. No stars, nebulae,
constellation lines, noise, or brightness were added. This viewer-only sky is
for orientation and is not registered to the simulator's rotating orbital frame
or used by the fixed-exposure RGB sensor cameras.

## Local processing and material limits

`worker/scripts/prepare-planetary-assets.py` reproduces the PNGs from the
checked-in `source/` files using the project's existing UV, NumPy and Pillow
environment. It does not download anything. `asset-manifest.json` records the
source URLs, dimensions, byte counts and SHA-256 hashes. Ordinary builds copy
only the prepared PNGs, this notice and the manifest, not the source JPEGs.

The planetary materials multiply these maps by the scenario's `RockSurface`
base color, use its configured roughness, and remain non-metallic. This is an
estimated visual material, not validation against STP photometry or BRDF.
Phobos UVs follow the existing synthetic physical terrain grid. Image features
are not a new measured shape model and do not alter vertices, gravity or
colliders. Mars uses the renderer's supplied physical radius. The celestial
map orientation is conventional and does not establish an absolute epoch.
