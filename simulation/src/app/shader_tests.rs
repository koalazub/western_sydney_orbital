//! CPU source-contract tests, not WGSL compilation or GPU/browser acceptance.
use super::{
    BASELINE_PBR_MARKER, PBR_TEXTURE_BINDINGS, baseline_pbr_bindings, baseline_pbr_source,
    pbr_token_spans,
};
use bevy::{
    asset::{AssetApp, AssetMetaCheck, AssetPath, AssetPlugin, io::Reader},
    camera::CameraPlugin,
    image::ImagePlugin,
    light::LightPlugin,
    mesh::MeshPlugin,
    pbr::PbrPlugin,
    prelude::{App, AssetServer, MinimalPlugins, Shader},
    render::{RenderApp, RenderPlugin, renderer::RenderDevice},
    shader::ShaderLoader,
    tasks::block_on,
};
use std::collections::BTreeMap;

fn headless_embedded_pbr_app() -> App {
    let mut app = App::new();
    app.add_plugins((
        MinimalPlugins,
        AssetPlugin {
            meta_check: AssetMetaCheck::Never,
            watch_for_changes_override: Some(false),
            ..Default::default()
        },
        ImagePlugin::default(),
        MeshPlugin,
        CameraPlugin,
        LightPlugin,
    ));
    // Bevy 0.20 has no standalone ShaderPlugin. Register only RenderPlugin's
    // shader asset/loader setup, without installing RenderPlugin itself.
    app.init_asset::<Shader>()
        .init_asset_loader::<ShaderLoader>()
        .add_plugins(PbrPlugin::default());
    assert!(!app.is_plugin_added::<RenderPlugin>());
    assert!(app.get_sub_app(RenderApp).is_none());
    assert!(!app.world().contains_resource::<RenderDevice>());
    // Build registers PbrPlugin's real embedded bytes synchronously. Do not
    // run, update or finish this app; no rendering or light/gizmo systems run.
    app
}

fn read_registered_wesl(app: &App, path: &'static str) -> String {
    let asset_path = AssetPath::from(path);
    assert_eq!(asset_path.source().as_str(), Some("embedded"));
    let server = app.world().resource::<AssetServer>();
    let source = server
        .get_source(asset_path.source())
        .expect("AssetPlugin must register the real embedded memory source");
    let bytes = block_on(async {
        let mut reader = source
            .reader()
            .read(asset_path.path())
            .await
            .unwrap_or_else(|error| panic!("PbrPlugin did not embed {path}: {error}"));
        let mut bytes = Vec::new();
        Reader::read_to_end(reader.as_mut(), &mut bytes)
            .await
            .unwrap_or_else(|error| panic!("Could not read embedded {path}: {error}"));
        bytes
    });
    String::from_utf8(bytes).expect("the pinned embedded WESL must be UTF-8")
}

fn tokens(source: &str) -> Vec<&str> {
    pbr_token_spans(source)
        .expect("the pinned embedded source must tokenize")
        .into_iter()
        .map(|span| &source[span])
        .collect()
}

fn canonical_expression(expression: &str) -> String {
    tokens(expression).join(" ")
}

fn opaque_alias_count(source: &str) -> usize {
    tokens(source)
        .windows(3)
        .filter(|window| {
            window[0] == "let"
                && matches!(
                    window[1],
                    "texture" | "sampler_" | "clearcoat_texture" | "clearcoat_sampler"
                )
                && window[2] == "="
        })
        .count()
}

#[derive(Debug)]
struct SampleCall {
    operation: String,
    arguments: Vec<String>,
}

fn sample_calls(source: &str) -> Vec<SampleCall> {
    let tokens = tokens(source);
    let mut calls = Vec::new();
    for (index, operation) in tokens.iter().enumerate() {
        if !operation.starts_with("textureSample") {
            continue;
        }
        assert_eq!(tokens.get(index + 1), Some(&"("));
        let mut arguments = Vec::new();
        let mut argument = Vec::new();
        let mut depth = 0;
        let mut closed = false;
        for &token in &tokens[index + 2..] {
            match token {
                ")" if depth == 0 => {
                    if !argument.is_empty() {
                        arguments.push(argument.join(" "));
                    }
                    closed = true;
                    break;
                }
                "," if depth == 0 => {
                    assert!(!argument.is_empty());
                    arguments.push(argument.join(" "));
                    argument.clear();
                }
                "(" | "[" | "{" => {
                    depth += 1;
                    argument.push(token);
                }
                ")" | "]" | "}" => {
                    assert!(depth > 0);
                    depth -= 1;
                    argument.push(token);
                }
                _ => argument.push(token),
            }
        }
        assert!(closed, "unterminated {operation} call");
        assert!(arguments.len() >= 3, "missing {operation} arguments");
        calls.push(SampleCall {
            operation: (*operation).to_owned(),
            arguments,
        });
    }
    calls
}

fn sampling_fingerprints(calls: &[SampleCall]) -> BTreeMap<(String, Vec<String>), usize> {
    let mut counts = BTreeMap::new();
    for call in calls {
        // Resource arguments change from aliases to direct bindings. All
        // remaining tokens, including UVs, gradients and mip bias, must stay.
        *counts
            .entry((call.operation.clone(), call.arguments[2..].to_vec()))
            .or_default() += 1;
    }
    counts
}

#[test]
fn actual_embedded_pbr_modules_inline_every_alias_and_preserve_sampling_operations() {
    let app = headless_embedded_pbr_app();
    let cases = [
        (
            "embedded://bevy_pbr/render/pbr_fragment.wesl",
            PBR_TEXTURE_BINDINGS,
            2,
        ),
        (
            "embedded://bevy_pbr/render/pbr_prepass_functions.wesl",
            &PBR_TEXTURE_BINDINGS[..1],
            1,
        ),
        (
            "embedded://bevy_pbr/render/pbr_prepass.wesl",
            &PBR_TEXTURE_BINDINGS[11..12],
            2,
        ),
    ];
    for (path, expected_bindings, samples_per_binding) in cases {
        let bindings = baseline_pbr_bindings(path).expect("real module must select its rewrite");
        assert_eq!(bindings, expected_bindings, "{path}");
        let original = read_registered_wesl(&app, path);
        assert!(!original.starts_with(BASELINE_PBR_MARKER), "{path}");
        // Each resource pair has texture and sampler lets in both branches.
        assert_eq!(opaque_alias_count(&original), bindings.len() * 4, "{path}");
        let original_calls = sample_calls(&original);
        assert_eq!(
            original_calls.len(),
            bindings.len() * samples_per_binding,
            "{path}"
        );
        for call in &original_calls {
            assert!(matches!(
                call.arguments[0].as_str(),
                "texture" | "clearcoat_texture"
            ));
            assert!(matches!(
                call.arguments[1].as_str(),
                "sampler_" | "clearcoat_sampler"
            ));
        }

        let rewritten = baseline_pbr_source(&original, bindings)
            .unwrap_or_else(|error| panic!("Pinned upstream {path} did not rewrite: {error}"))
            .expect("unmodified pinned upstream source must not become a no-op");
        assert!(rewritten.starts_with(BASELINE_PBR_MARKER));
        assert_eq!(opaque_alias_count(&rewritten), 0, "{path}");
        assert_eq!(
            baseline_pbr_source(&rewritten, bindings).unwrap(),
            None,
            "{path}"
        );

        let rewritten_calls = sample_calls(&rewritten);
        // The raw WESL contains one sampling body per binding model. WESL
        // selects one branch later; this test does not run that compiler.
        let expected_fingerprints = sampling_fingerprints(&original_calls)
            .into_iter()
            .map(|(call, count)| (call, count * 2))
            .collect::<BTreeMap<_, _>>();
        assert_eq!(
            sampling_fingerprints(&rewritten_calls),
            expected_fingerprints,
            "{path}"
        );
        let mut actual_resources = BTreeMap::new();
        for call in &rewritten_calls {
            *actual_resources
                .entry((call.arguments[0].clone(), call.arguments[1].clone()))
                .or_insert(0) += 1;
        }
        let mut expected_resources = BTreeMap::new();
        for &(texture, sampler) in bindings {
            for (texture, sampler) in [
                (
                    format!("pbr_bindings::{texture}"),
                    format!("pbr_bindings::{sampler}"),
                ),
                (
                    format!("bindless_textures_2d[material_indices[slot].{texture}]"),
                    format!("bindless_samplers_filtering[material_indices[slot].{sampler}]"),
                ),
            ] {
                expected_resources.insert(
                    (
                        canonical_expression(&texture),
                        canonical_expression(&sampler),
                    ),
                    samples_per_binding,
                );
            }
        }
        assert_eq!(
            actual_resources, expected_resources,
            "resource binding mismatch in {path}"
        );
        assert_eq!(
            read_registered_wesl(&app, path),
            original,
            "test mutated upstream bytes"
        );

        let changed = original.replacen(
            &format!("pbr_bindings::{}", bindings[0].0),
            "pbr_bindings::unexpected_upstream_resource",
            1,
        );
        assert_ne!(changed, original);
        assert!(
            baseline_pbr_source(&changed, bindings).is_err(),
            "upstream drift in {path}"
        );
    }
}
