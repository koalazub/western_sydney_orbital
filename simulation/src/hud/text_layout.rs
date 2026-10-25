//! Match HUD text's intrinsic measure and final glyph layout.
//!
//! Bevy 0.20's default text measure treats an explicit minimum as a preferred
//! width. Parley also hangs a breaking space past its wrap advance, while Bevy
//! reports `full_width`, including that space. Use actual shaped whitespace to
//! reserve a wrap gutter, in physical pixels, in both measurement and drawing.

use bevy::{
    prelude::*,
    text::{
        ComputedTextBlock, FontAtlasSet, FontHinting, LineBreak, ScaleCx, TextBounds,
        TextLayoutInfo, TextMeasureInfo, TextPipeline,
    },
    ui::{AvailableSpace, ContentSize, Measure, MeasureArgs, NodeMeasure, UiSystems, widget},
};

#[derive(Component, Default)]
pub(super) struct HudTextLayout(Option<WrapMetrics>);

#[derive(Clone, Copy)]
struct WrapMetrics {
    minimum: f32,
    maximum: Vec2,
    gutter: f32,
}

impl WrapMetrics {
    fn wrap_width(self, allocated: f32) -> f32 {
        // Taffy rounds edges after measuring. Its final width can be the floor
        // of the fractional allocation, so measure no wider than that floor.
        let allocated = allocated.floor().max(0.0);
        if allocated >= self.maximum.x {
            allocated
        } else {
            (allocated - self.gutter).max(0.0)
        }
    }
}

struct HudTextMeasure {
    info: TextMeasureInfo,
    metrics: WrapMetrics,
    // UiSurface only supplies MeasureArgs::buffer to NodeMeasure::Text, so a
    // custom measure retains the real native-shaped buffer, not an estimate.
    block: ComputedTextBlock,
}

fn clamp(value: f32, minimum: Option<f32>, maximum: Option<f32>) -> f32 {
    let value = minimum.map_or(value, |minimum| value.max(minimum));
    maximum.map_or(value, |maximum| value.min(maximum))
}

impl Measure for HudTextMeasure {
    fn measure(&mut self, args: MeasureArgs) -> Vec2 {
        let width = args.resolve_width();
        let height = args.resolve_height();
        // A minimum is a bound, not an intrinsic width of zero. These tagged
        // text leaves have no padding or border; the button owns those insets.
        let intrinsic = match args.available_width {
            AvailableSpace::Definite(available) => available.min(self.metrics.maximum.x),
            AvailableSpace::MinContent => self.metrics.minimum,
            AvailableSpace::MaxContent => self.metrics.maximum.x,
        };
        let allocated = clamp(
            args.known_width.or(width.preferred).unwrap_or(intrinsic),
            width.min,
            width.max,
        )
        .max(0.0);
        let measured = self.info.compute_size(
            TextBounds::new_horizontal(self.metrics.wrap_width(allocated)),
            &mut self.block,
            args.font_system,
        );
        Vec2::new(
            allocated,
            clamp(
                args.known_height.or(height.preferred).unwrap_or(measured.y),
                height.min,
                height.max,
            ),
        )
        .ceil()
    }
}

pub(super) fn install(app: &mut App) {
    app.add_systems(
        PostUpdate,
        install_measures
            .in_set(UiSystems::Content)
            .after(widget::measure_text_system)
            .before(UiSystems::Layout),
    )
    .add_systems(
        PostUpdate,
        layout_glyphs
            .in_set(UiSystems::PostLayout)
            .after(widget::text_system),
    );
}

type MeasureLayout = (
    Entity,
    &'static TextLayout,
    &'static ComputedTextBlock,
    &'static mut ContentSize,
    &'static mut HudTextLayout,
);

fn install_measures(mut texts: Query<MeasureLayout>) {
    for (entity, layout, block, mut content, mut hud) in &mut texts {
        // Native measure_text_system leaves a freshly shaped, unbounded buffer.
        // Layout consumes ContentSize with change detection too, so that flag
        // alone is not enough to distinguish a new measure from a consumed one.
        let buffer = block.buffer();
        if layout.linebreak == LineBreak::NoWrap
            || !content.is_changed()
            || buffer.layout_max_advance() < f32::MAX
        {
            continue;
        }
        let maximum = Vec2::new(buffer.full_width(), buffer.height()).ceil();
        let mut whitespace = 0.0_f32;
        let mut widest_whitespace = 0.0_f32;
        for line in buffer.lines() {
            for run in line.runs() {
                for cluster in run.clusters() {
                    if cluster.is_space_or_nbsp() {
                        whitespace += cluster.advance();
                        widest_whitespace = widest_whitespace.max(whitespace);
                    } else {
                        whitespace = 0.0;
                    }
                }
            }
            whitespace = 0.0;
        }
        let metrics = WrapMetrics {
            minimum: buffer.calculate_content_widths().min.ceil(),
            maximum,
            // One physical pixel covers Bevy's final ceil, not a CSS-sized
            // guess that changes meaning with device or UI scale.
            gutter: widest_whitespace.ceil() + 1.0,
        };
        hud.0 = Some(metrics);
        content.set(NodeMeasure::Custom(Box::new(HudTextMeasure {
            info: TextMeasureInfo {
                min: Vec2::new(metrics.minimum, maximum.y),
                max: maximum,
                entity,
            },
            metrics,
            block: block.clone(),
        })));
    }
}

type GlyphLayout = (
    Ref<'static, ComputedNode>,
    &'static TextLayout,
    Ref<'static, HudTextLayout>,
    Ref<'static, FontHinting>,
    &'static mut TextLayoutInfo,
    &'static mut ComputedTextBlock,
);

fn layout_glyphs(
    mut texts: Query<GlyphLayout>,
    mut pipeline: ResMut<TextPipeline>,
    mut atlases: ResMut<FontAtlasSet>,
    mut images: ResMut<Assets<Image>>,
    mut scale: ResMut<ScaleCx>,
) {
    for (node, layout, hud, hinting, mut info, mut block) in &mut texts {
        let Some(metrics) = hud.0 else {
            continue;
        };
        if layout.linebreak == LineBreak::NoWrap
            || !(node.is_changed() || hud.is_changed() || hinting.is_changed())
        {
            continue;
        }
        let content = node.content_box().size();
        pipeline
            .update_text_layout_info(
                &mut info,
                &mut atlases,
                &mut images,
                &mut block,
                &mut scale,
                TextBounds::new(metrics.wrap_width(content.x), content.y),
                layout.justify,
                *hinting,
            )
            .expect("HUD text uses the same loaded font and atlas as native UI text");
    }
}
