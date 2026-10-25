//! Real spacecraft surface imagery and a viewer-only catalog-based sky.
//! Provenance, processing limits and redistribution terms ship with the PNGs.
use bevy::{
    asset::{AssetEventSystems, LoadState, RenderAssetUsages},
    image::{ImageAddressMode, ImageLoaderSettings, ImageSampler, ImageSamplerDescriptor},
    prelude::*,
    render::{
        RenderApp,
        extract_resource::{ExtractResource, ExtractResourcePlugin},
        render_asset::RenderAssets,
        render_resource::{
            TextureDimension, TextureFormat, TextureViewDescriptor, TextureViewDimension,
        },
        texture::GpuImage,
    },
};

pub(crate) const PHOBOS_ALBEDO_PATH: &str = "textures/phobos-viking-albedo.png";
pub(crate) const MARS_ALBEDO_PATH: &str = "textures/mars-viking-albedo.png";
pub(crate) const STARFIELD_PATH: &str = "textures/stars-nasa-svs-cube.png";
pub(crate) const STARFIELD_FACE_SIZE: u32 = 512;

#[derive(Clone, Debug, PartialEq, Eq)]
enum SurfaceMapStatus {
    Loading,
    Ready,
    Failed(String),
}

/// Extracted to the render app so primary-view readiness can require all maps.
/// A missing, undecodable or malformed map is never replaced by a flat color.
#[derive(Resource, Clone, ExtractResource)]
#[extract_app(RenderApp)]
pub(crate) struct SurfaceMaps {
    phobos: Handle<Image>,
    mars: Handle<Image>,
    stars: Handle<Image>,
    status: SurfaceMapStatus,
}

impl SurfaceMaps {
    /// `Ok(false)` means loading; `Err` is fatal and must fail renderer startup.
    pub(crate) fn loading_status(&self) -> Result<bool, &str> {
        match &self.status {
            SurfaceMapStatus::Loading => Ok(false),
            SurfaceMapStatus::Ready => Ok(true),
            SurfaceMapStatus::Failed(error) => Err(error),
        }
    }

    pub(crate) fn gpu_ready(&self, images: &RenderAssets<GpuImage>) -> bool {
        [&self.phobos, &self.mars, &self.stars]
            .into_iter()
            .all(|handle| images.get(handle).is_some())
    }
}

fn surface_image(asset_server: &AssetServer, path: &'static str) -> Handle<Image> {
    asset_server
        .load_builder()
        .with_settings(|settings: &mut ImageLoaderSettings| {
            settings.is_srgb = true;
            settings.asset_usage = RenderAssetUsages::default();
            settings.sampler = ImageSampler::Descriptor(ImageSamplerDescriptor {
                address_mode_u: ImageAddressMode::Repeat,
                address_mode_v: ImageAddressMode::ClampToEdge,
                ..ImageSamplerDescriptor::linear()
            });
        })
        .load(path)
}

fn surface_material(
    asset_server: &AssetServer,
    path: &'static str,
    surface: crate::scenario::RockSurface,
) -> StandardMaterial {
    let [r, g, b] = surface.base_color_srgb;
    StandardMaterial {
        base_color: Color::srgb(r, g, b),
        base_color_texture: Some(surface_image(asset_server, path)),
        metallic: 0.0,
        perceptual_roughness: surface.perceptual_roughness,
        ..default()
    }
}

pub(crate) fn phobos_material(asset_server: &AssetServer) -> StandardMaterial {
    surface_material(
        asset_server,
        PHOBOS_ALBEDO_PATH,
        crate::scenario::settings().lighting.phobos_surface,
    )
}

pub(crate) fn mars_material(asset_server: &AssetServer) -> StandardMaterial {
    surface_material(
        asset_server,
        MARS_ALBEDO_PATH,
        crate::scenario::settings().lighting.mars_surface,
    )
}

/// Attach this cube only to the main debug camera, never a sensor camera.
pub(crate) fn starfield_image(asset_server: &AssetServer) -> Handle<Image> {
    asset_server
        .load_builder()
        .with_settings(|settings: &mut ImageLoaderSettings| {
            settings.is_srgb = true;
            settings.asset_usage = RenderAssetUsages::default();
            settings.sampler = ImageSampler::Descriptor(ImageSamplerDescriptor::linear());
        })
        .load(STARFIELD_PATH)
}

pub(crate) fn install(app: &mut App) {
    let asset_server = app.world().resource::<AssetServer>();
    let maps = SurfaceMaps {
        phobos: surface_image(asset_server, PHOBOS_ALBEDO_PATH),
        mars: surface_image(asset_server, MARS_ALBEDO_PATH),
        stars: starfield_image(asset_server),
        status: SurfaceMapStatus::Loading,
    };
    app.insert_resource(maps)
        .add_plugins(ExtractResourcePlugin::<SurfaceMaps>::default())
        // AssetServer inserts decoded images in PreUpdate. Convert the actual
        // loaded PNG before asset events and render extraction, including reloads.
        .add_systems(PostUpdate, prepare_surface_maps.before(AssetEventSystems));
}

fn validate_surface(image: &Image, path: &str, width: u32, height: u32) -> Result<(), String> {
    let descriptor = &image.texture_descriptor;
    if descriptor.dimension != TextureDimension::D2
        || descriptor.size.width != width
        || descriptor.size.height != height
        || descriptor.size.depth_or_array_layers != 1
        || descriptor.format != TextureFormat::Rgba8UnormSrgb
        || image
            .data
            .as_ref()
            .is_none_or(|data| data.len() != (width * height * 4) as usize)
    {
        return Err(format!(
            "Invalid planetary texture {path}; expected sRGB {width}x{height} RGBA"
        ));
    }
    Ok(())
}

fn validate_starfield(image: &Image) -> Result<(), String> {
    let descriptor = &image.texture_descriptor;
    if descriptor.dimension != TextureDimension::D2
        || descriptor.format != TextureFormat::Rgba8UnormSrgb
        || descriptor.size.width != STARFIELD_FACE_SIZE
        || image.data.as_ref().is_none_or(|data| {
            data.len() != (STARFIELD_FACE_SIZE * STARFIELD_FACE_SIZE * 6 * 4) as usize
        })
    {
        return Err(format!("Invalid star-map texture {STARFIELD_PATH}"));
    }
    match (
        descriptor.size.height,
        descriptor.size.depth_or_array_layers,
    ) {
        (height, 1) if height == STARFIELD_FACE_SIZE * 6 => Ok(()),
        (STARFIELD_FACE_SIZE, 6) => Ok(()),
        _ => Err("Star map must contain exactly six square faces".into()),
    }
}

fn prepare_starfield(image: &mut Image) -> Result<(), String> {
    validate_starfield(image)?;
    if image.texture_descriptor.size.depth_or_array_layers == 1 {
        image
            .reinterpret_stacked_2d_as_array(6)
            .map_err(|error| format!("Invalid star-map cube layout: {error}"))?;
    }
    image.texture_view_descriptor = Some(TextureViewDescriptor {
        dimension: Some(TextureViewDimension::Cube),
        ..default()
    });
    Ok(())
}

fn prepare_surface_maps(
    asset_server: Res<AssetServer>,
    mut images: ResMut<Assets<Image>>,
    mut maps: ResMut<SurfaceMaps>,
) {
    // Preserve a fatal failure even if an unrelated asset loads later.
    if matches!(maps.status, SurfaceMapStatus::Failed(_)) {
        return;
    }
    let handles = [
        (PHOBOS_ALBEDO_PATH, maps.phobos.clone()),
        (MARS_ALBEDO_PATH, maps.mars.clone()),
        (STARFIELD_PATH, maps.stars.clone()),
    ];
    let mut loaded = true;
    for (path, handle) in &handles {
        match asset_server.get_load_state(handle.id()) {
            Some(LoadState::Failed(error)) => {
                maps.status = SurfaceMapStatus::Failed(format!("Texture {path} failed: {error}"));
                return;
            }
            Some(LoadState::Loaded) => {}
            _ => loaded = false,
        }
    }
    if !loaded {
        maps.status = SurfaceMapStatus::Loading;
        return;
    }
    let result = (|| {
        for (path, handle, width, height) in [
            (PHOBOS_ALBEDO_PATH, &maps.phobos, 2048, 1024),
            (MARS_ALBEDO_PATH, &maps.mars, 1440, 720),
        ] {
            let image = images
                .get(handle)
                .ok_or_else(|| format!("Loaded texture {path} is absent"))?;
            validate_surface(image, path, width, height)?;
        }
        let stars = images.get(&maps.stars).ok_or("Loaded star map is absent")?;
        validate_starfield(stars)?;
        if stars.texture_descriptor.size.depth_or_array_layers != 6
            || stars
                .texture_view_descriptor
                .as_ref()
                .and_then(|view| view.dimension)
                != Some(TextureViewDimension::Cube)
        {
            let mut stars = images
                .get_mut(&maps.stars)
                .ok_or("Loaded star map is absent")?;
            prepare_starfield(&mut stars)?;
        }
        Ok::<(), String>(())
    })();
    maps.status = match result {
        Ok(()) => SurfaceMapStatus::Ready,
        Err(error) => SurfaceMapStatus::Failed(error),
    };
}

#[cfg(test)]
mod tests {
    use super::*;
    use bevy::image::{CompressedImageFormats, ImageType};

    fn decode(bytes: &[u8]) -> Image {
        Image::from_buffer(
            bytes,
            ImageType::Extension("png"),
            CompressedImageFormats::NONE,
            true,
            ImageSampler::Default,
            RenderAssetUsages::default(),
        )
        .expect("checked-in production PNG must decode with the configured Bevy features")
    }

    #[test]
    fn actual_planetary_pngs_decode_with_dimensions_and_surface_detail() {
        for (bytes, path, width, height) in [
            (
                include_bytes!("../../../worker/web/assets/textures/phobos-viking-albedo.png")
                    .as_slice(),
                PHOBOS_ALBEDO_PATH,
                2048,
                1024,
            ),
            (
                include_bytes!("../../../worker/web/assets/textures/mars-viking-albedo.png")
                    .as_slice(),
                MARS_ALBEDO_PATH,
                1440,
                720,
            ),
        ] {
            let image = decode(bytes);
            validate_surface(&image, path, width, height).unwrap();
            let data = image.data.as_ref().unwrap();
            let reference = &data[..3];
            let changed = data
                .chunks_exact(4)
                .filter(|pixel| &pixel[..3] != reference)
                .count();
            assert!(
                changed > (width * height / 2) as usize,
                "map lost its source imagery"
            );
            assert!(data.chunks_exact(4).all(|pixel| pixel[3] == 255));
        }
    }

    #[test]
    fn actual_star_map_becomes_a_six_face_cube_without_changing_pixels() {
        let mut image = decode(include_bytes!(
            "../../../worker/web/assets/textures/stars-nasa-svs-cube.png"
        ));
        let original_data = image.data.clone();
        assert_eq!(image.width(), 512);
        assert_eq!(image.height(), 3072);
        prepare_starfield(&mut image).unwrap();
        assert_eq!(image.width(), 512);
        assert_eq!(image.height(), 512);
        assert_eq!(image.texture_descriptor.size.depth_or_array_layers, 6);
        assert_eq!(
            image.texture_view_descriptor.as_ref().unwrap().dimension,
            Some(TextureViewDimension::Cube)
        );
        assert_eq!(image.data, original_data);
        prepare_starfield(&mut image).unwrap();
        assert_eq!(image.data, original_data);
        image.texture_descriptor.size.depth_or_array_layers = 5;
        assert!(prepare_starfield(&mut image).is_err());
    }

    #[test]
    fn loading_and_failed_textures_cannot_report_ready() {
        let mut maps = SurfaceMaps {
            phobos: default(),
            mars: default(),
            stars: default(),
            status: SurfaceMapStatus::Loading,
        };
        assert_eq!(maps.loading_status(), Ok(false));
        maps.status = SurfaceMapStatus::Failed("real texture decode failed".into());
        assert_eq!(maps.loading_status(), Err("real texture decode failed"));
        maps.status = SurfaceMapStatus::Ready;
        assert_eq!(maps.loading_status(), Ok(true));
    }

    #[test]
    #[cfg(not(target_arch = "wasm32"))]
    fn production_asset_server_loads_and_prepares_the_real_maps_without_a_renderer() {
        use bevy::asset::{AssetApp, AssetMetaCheck, AssetPlugin};
        let mut app = App::new();
        app.add_plugins((
            MinimalPlugins,
            AssetPlugin {
                file_path: format!("{}/../worker/web/assets", env!("CARGO_MANIFEST_DIR")),
                meta_check: AssetMetaCheck::Never,
                watch_for_changes_override: Some(false),
                ..default()
            },
            bevy::image::ImagePlugin::default(),
        ));
        // TexturePlugin normally registers this loader after discovering GPU
        // compressed-format support. These production PNGs are uncompressed;
        // register the same decoder without creating a render device.
        app.register_asset_loader(bevy::image::ImageLoader::new(CompressedImageFormats::NONE));
        install(&mut app);
        app.finish();
        app.cleanup();
        let deadline = std::time::Instant::now() + std::time::Duration::from_secs(10);
        loop {
            app.update();
            match app.world().resource::<SurfaceMaps>().loading_status() {
                Ok(true) => break,
                Err(error) => panic!("Production texture loading failed: {error}"),
                Ok(false) => assert!(
                    std::time::Instant::now() < deadline,
                    "Real texture loading timed out"
                ),
            }
            std::thread::yield_now();
        }
        let maps = app.world().resource::<SurfaceMaps>();
        let server = app.world().resource::<AssetServer>();
        assert_eq!(
            phobos_material(server).base_color_texture.as_ref(),
            Some(&maps.phobos)
        );
        assert_eq!(
            mars_material(server).base_color_texture.as_ref(),
            Some(&maps.mars)
        );
        assert_eq!(starfield_image(server), maps.stars);
        let images = app.world().resource::<Assets<Image>>();
        validate_surface(
            images.get(&maps.phobos).unwrap(),
            PHOBOS_ALBEDO_PATH,
            2048,
            1024,
        )
        .unwrap();
        validate_surface(images.get(&maps.mars).unwrap(), MARS_ALBEDO_PATH, 1440, 720).unwrap();
        let stars = images.get(&maps.stars).unwrap();
        assert_eq!(stars.texture_descriptor.size.depth_or_array_layers, 6);
        assert_eq!(
            stars.texture_view_descriptor.as_ref().unwrap().dimension,
            Some(TextureViewDimension::Cube)
        );
        assert!(!maps.gpu_ready(&RenderAssets::<GpuImage>::default()));
        assert!(app.get_sub_app(RenderApp).is_none());
    }
}
