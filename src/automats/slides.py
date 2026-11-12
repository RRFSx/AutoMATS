"""Build a local PowerPoint deck with an interactive Overview slide from the recipe's resolved PNG rows."""

from __future__ import annotations

from datetime import datetime
from io import BytesIO
import os
from pathlib import Path
import re
import sys
import tempfile
from typing import Any, Dict, List, Optional, Tuple

from automats.config import Plan


class SlideError(ValueError):
    pass


NAMED_COLORS: Dict[str, Tuple[int, int, int]] = {
    # Standard MATS / CSS curve colors
    "red": (255, 0, 0),
    "blue": (0, 0, 255),
    "orange": (255, 165, 0),
    "green": (0, 128, 0),
    "lime": (0, 255, 0),
    "purple": (128, 0, 128),
    "magenta": (255, 0, 255),
    "fuchsia": (255, 0, 255),
    "cyan": (0, 255, 255),
    "aqua": (0, 255, 255),
    "teal": (0, 128, 128),
    "yellow": (255, 255, 0),
    "gold": (255, 215, 0),
    "brown": (165, 42, 42),
    "pink": (255, 192, 203),
    "gray": (128, 128, 128),
    "grey": (128, 128, 128),
    "black": (0, 0, 0),
    "white": (255, 255, 255),
    "olive": (128, 128, 0),
    "navy": (0, 0, 128),
    "maroon": (128, 0, 0),
    "violet": (238, 130, 238),
    "darkorange": (255, 140, 0),
    "coral": (255, 127, 80),
    "indigo": (75, 0, 130),
    "turquoise": (64, 224, 208),
    "dodgerblue": (30, 144, 255),
    "royalblue": (65, 105, 225),
    "steelblue": (70, 130, 180),
    "forestgreen": (34, 139, 34),
    "darkgreen": (0, 100, 0),
    "darkblue": (0, 0, 139),
    "darkred": (139, 0, 0),
    "crimson": (220, 20, 60),
}

PREFERRED_SECTIONS = [
    "Surface",
    "Upper Air",
    "Radar Refl",
    "Ceiling",
    "Visibility",
    "pygraf",
]


def parse_color_to_rgb(color_str: str) -> Any:
    """Parses named color, hex code, or rgb() string into pptx RGBColor."""
    try:
        from pptx.dml.color import RGBColor
    except (ImportError, Exception):
        class RGBColor(tuple):
            def __new__(cls, r, g, b):
                return super().__new__(cls, (int(r), int(g), int(b)))

    s = str(color_str).strip().lower()
    if s in NAMED_COLORS:
        r, g, b = NAMED_COLORS[s]
        return RGBColor(r, g, b)
    if s.startswith("#"):
        hex_val = s.lstrip("#")
        if len(hex_val) == 6:
            return RGBColor(int(hex_val[:2], 16), int(hex_val[2:4], 16), int(hex_val[4:6], 16))
        if len(hex_val) == 3:
            return RGBColor(int(hex_val[0] * 2, 16), int(hex_val[1] * 2, 16), int(hex_val[2] * 2, 16))
    m = re.match(r"rgb\s*\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*\)", s)
    if m:
        return RGBColor(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    return RGBColor(70, 80, 95)


def derive_deck_title(plan: Plan) -> str:
    """Determines a context-aware title from the plan, experiments, and recipe file."""
    if getattr(plan, "title", None) and plan.title.strip():
        return plan.title.strip()

    recipe_name = plan.recipe_path.stem.lower() if plan.recipe_path else ""
    exp_names = " ".join(e.name.lower() for e in plan.experiments)

    cluster = None
    for exp in plan.experiments:
        if exp.hpc_cluster:
            cluster = exp.hpc_cluster
            break

    cluster_str = ""
    if cluster:
        if cluster.lower() == "gaeac6":
            cluster_fmt = "GaeaC6"
        elif cluster.lower() == "gaeac7":
            cluster_fmt = "GaeaC7"
        elif cluster.lower() in ("hpc",):
            cluster_fmt = "HPC"
        else:
            cluster_fmt = cluster.capitalize()
        cluster_str = f" on {cluster_fmt}"

    # Check for Baseline / Retrospective
    if any(k in recipe_name for k in ("baseline", "retro")) or any(k in exp_names for k in ("ret_", "retv", "retro")):
        return f"Baseline Retro{cluster_str}"

    # Check for Realtime
    if any(k in recipe_name for k in ("rt", "realtime")):
        if "rrfs" in recipe_name or "rrfs" in exp_names:
            return f"Realtime RRFSv2 runs{cluster_str}"
        return f"Realtime Verification runs{cluster_str}"

    if plan.experiments:
        labels = [e.display_name or e.name for e in plan.experiments]
        return f"{' vs '.join(labels[:3])} Verification{cluster_str}"

    if plan.recipe_path:
        clean_stem = plan.recipe_path.stem.replace("_", " ").title()
        return f"{clean_stem} Verification"

    return "Verification Overview"


def derive_subtitle(plan: Plan) -> str:
    """Builds a descriptive subtitle with date range and generator metadata."""
    parts = []
    start_dt = None
    end_dt = None
    for job in plan.jobs:
        if job.start and (start_dt is None or job.start < start_dt):
            start_dt = job.start
        if job.end and (end_dt is None or job.end > end_dt):
            end_dt = job.end
    if start_dt and end_dt:
        s_clean = start_dt.split("T")[0] if "T" in start_dt else start_dt[:10]
        e_clean = end_dt.split("T")[0] if "T" in end_dt else end_dt[:10]
        parts.append(f"Evaluation Period: {s_clean} to {e_clean}")

    recipe_name = plan.recipe_path.name if plan.recipe_path else ""
    if recipe_name:
        parts.append(f"Powered by AutoMATS , Recipe: {recipe_name}")
    else:
        parts.append("Powered by AutoMATS")
    return "   •   ".join(parts)


def classify_slide_section(title: str, plot_group: str = "") -> str:
    """Classifies a slide into a navigation category (Surface, Upper Air, Radar Refl, etc.)."""
    combined = f"{title} {plot_group}".lower()
    if any(k in combined for k in ("radar", "mrms", "cref", "refl")):
        return "Radar Refl"
    if "ceiling" in combined:
        return "Ceiling"
    if any(k in combined for k in ("vis", "visibility")):
        return "Visibility"
    if any(k in combined for k in ("ua", "upper air", "upperair", "profile", "raob")):
        return "Upper Air"
    if any(k in combined for k in ("sfc", "surface")):
        return "Surface"
    if any(k in combined for k in ("radiation", "solar", "flux")):
        return "Radiation"
    return "Other"


def add_overview_backlink(slide: Any, overview_slide: Any, deck_width: Any):
    """Adds a compact '⌂ Overview' button to the top-right corner of a content slide."""
    from pptx.dml.color import RGBColor
    from pptx.enum.shapes import MSO_SHAPE
    from pptx.enum.text import MSO_ANCHOR, PP_ALIGN
    from pptx.util import Inches, Pt

    link_w = Inches(1.25)
    link_h = Inches(0.32)
    link_left = deck_width - Inches(0.25) - link_w
    link_top = Inches(0.2)

    btn = slide.shapes.add_shape(MSO_SHAPE.ROUNDED_RECTANGLE, link_left, link_top, link_w, link_h)
    btn.fill.solid()
    btn.fill.fore_color.rgb = RGBColor(245, 248, 252)
    btn.line.color.rgb = RGBColor(190, 205, 225)
    btn.line.width = Pt(1)
    btn.click_action.target_slide = overview_slide

    btf = btn.text_frame
    btf.word_wrap = False
    btf.vertical_anchor = MSO_ANCHOR.MIDDLE
    bp = btf.paragraphs[0]
    bp.text = "⌂ Overview"
    bp.alignment = PP_ALIGN.CENTER
    bp.font.size = Pt(10)
    bp.font.bold = True
    bp.font.color.rgb = RGBColor(40, 75, 115)


def populate_overview_slide(
    overview_slide: Any,
    plan: Plan,
    section_first_slide: Dict[str, Any],
):
    """Renders context-aware title, evident legend cards, and quick navigation hyperlinks on the overview slide."""
    from pptx.dml.color import RGBColor
    from pptx.enum.shapes import MSO_SHAPE
    from pptx.enum.text import MSO_ANCHOR, PP_ALIGN
    from pptx.util import Inches, Pt

    # 1. Title and Subtitle Box
    title_box = overview_slide.shapes.add_textbox(Inches(0.6), Inches(0.38), Inches(12.133), Inches(1.1))
    tf = title_box.text_frame
    tf.word_wrap = True
    tf.margin_top = tf.margin_bottom = tf.margin_left = tf.margin_right = 0
    p1 = tf.paragraphs[0]
    p1.text = derive_deck_title(plan)
    p1.font.size = Pt(28)
    p1.font.bold = True
    p1.font.color.rgb = RGBColor(26, 43, 76)  # Deep Navy

    p2 = tf.add_paragraph()
    p2.text = derive_subtitle(plan)
    p2.font.size = Pt(11.5)
    p2.font.color.rgb = RGBColor(90, 105, 120)

    # 2. Quick Navigation Bar
    active_sections = [s for s in PREFERRED_SECTIONS if s in section_first_slide]
    for s in section_first_slide:
        if s not in active_sections and s != "Other":
            active_sections.append(s)

    if active_sections:
        nav_label = overview_slide.shapes.add_textbox(Inches(0.6), Inches(1.55), Inches(12.133), Inches(0.28))
        ntf = nav_label.text_frame
        ntf.margin_top = ntf.margin_bottom = ntf.margin_left = ntf.margin_right = 0
        np = ntf.paragraphs[0]
        np.text = "QUICK NAVIGATION (Click Section to Jump):"
        np.font.size = Pt(10.5)
        np.font.bold = True
        np.font.color.rgb = RGBColor(9, 105, 218)

        nav_left = Inches(0.6)
        nav_top = Inches(1.88)
        nav_total_w = Inches(12.133)
        btn_h = Inches(0.46)
        btn_gap = Inches(0.12)
        btn_w = (nav_total_w - btn_gap * (len(active_sections) - 1)) / len(active_sections)

        for idx, sec in enumerate(active_sections):
            bx = nav_left + idx * (btn_w + btn_gap)
            btn = overview_slide.shapes.add_shape(MSO_SHAPE.ROUNDED_RECTANGLE, bx, nav_top, btn_w, btn_h)
            btn.fill.solid()
            btn.fill.fore_color.rgb = RGBColor(238, 244, 252)
            btn.line.color.rgb = RGBColor(41, 128, 185)
            btn.line.width = Pt(1.5)
            btn.click_action.target_slide = section_first_slide[sec]

            btf = btn.text_frame
            btf.word_wrap = False
            btf.vertical_anchor = MSO_ANCHOR.MIDDLE
            bp = btf.paragraphs[0]
            bp.text = sec
            bp.alignment = PP_ALIGN.CENTER
            bp.font.size = Pt(11.5 if len(active_sections) >= 6 else 12.5)
            bp.font.bold = True
            bp.font.color.rgb = RGBColor(24, 76, 120)

    # 3. Evident Legend Area
    card_top = Inches(2.6)
    card_h = Inches(4.5)
    left_x = Inches(0.6)
    left_w = Inches(7.5)
    right_x = Inches(8.35)
    right_w = Inches(4.383)

    # 3A. Left Container: Evaluated Experiments & Models
    box1 = overview_slide.shapes.add_shape(MSO_SHAPE.ROUNDED_RECTANGLE, left_x, card_top, left_w, card_h)
    box1.fill.solid()
    box1.fill.fore_color.rgb = RGBColor(248, 250, 252)
    box1.line.color.rgb = RGBColor(218, 225, 233)
    box1.line.width = Pt(1)

    h1 = overview_slide.shapes.add_textbox(left_x + Inches(0.25), card_top + Inches(0.18), left_w - Inches(0.5), Inches(0.35))
    h1_tf = h1.text_frame
    h1_tf.margin_top = h1_tf.margin_bottom = h1_tf.margin_left = h1_tf.margin_right = 0
    hp1 = h1_tf.paragraphs[0]
    hp1.text = "EVALUATED EXPERIMENTS & MODELS"
    hp1.font.size = Pt(11.5)
    hp1.font.bold = True
    hp1.font.color.rgb = RGBColor(9, 105, 218)

    if plan.experiments:
        exp_count = len(plan.experiments)
        avail_h = card_h - Inches(0.7)
        item_gap = Inches(0.12)
        item_h = min(Inches(1.15), (avail_h - item_gap * (exp_count - 1)) / exp_count)

        for i, exp in enumerate(plan.experiments):
            ey = card_top + Inches(0.58) + i * (item_h + item_gap)
            c_rgb = parse_color_to_rgb(exp.color)

            # Experiment text block (full container width, no swatch)
            etb = overview_slide.shapes.add_textbox(left_x + Inches(0.3), ey, left_w - Inches(0.55), item_h)
            etf = etb.text_frame
            etf.margin_top = etf.margin_bottom = etf.margin_left = etf.margin_right = 0
            etf.word_wrap = True

            ep1 = etf.paragraphs[0]
            ep1.text = (
                f"{exp.short_name} ({exp.name})"
                if exp.short_name and exp.short_name != exp.name
                else exp.name
            )
            ep1.font.size = Pt(13)
            ep1.font.bold = True
            ep1.font.color.rgb = c_rgb
            for r in ep1.runs:
                r.font.size = Pt(13)
                r.font.bold = True
                r.font.color.rgb = c_rgb

            details = []
            if exp.hpc_cluster:
                details.append(f"HPC Cluster: {exp.hpc_cluster}")
            if exp.color:
                details.append(f"Curve Color: {exp.color}")

            ep2 = etf.add_paragraph()
            ep2.text = "   •   ".join(details)
            ep2.font.size = Pt(9.5)
            ep2.font.color.rgb = RGBColor(90, 100, 115)
            for r in ep2.runs:
                r.font.size = Pt(9.5)
                r.font.color.rgb = RGBColor(90, 100, 115)

            if exp.remote_comroot:
                ep3 = etf.add_paragraph()
                ep3.text = f"COMROOT: {exp.remote_comroot}"
                ep3.font.size = Pt(8.5)
                ep3.font.color.rgb = RGBColor(115, 125, 140)
                for r in ep3.runs:
                    r.font.size = Pt(8.5)
                    r.font.color.rgb = RGBColor(115, 125, 140)
    else:
        etb = overview_slide.shapes.add_textbox(left_x + Inches(0.3), card_top + Inches(0.7), left_w - Inches(0.6), Inches(1.0))
        ep = etb.text_frame.paragraphs[0]
        ep.text = "Figures loaded from local verification results."
        ep.font.size = Pt(12)
        ep.font.color.rgb = RGBColor(100, 110, 125)

    # 3B. Right Container: Verification Metrics & Truth
    box2 = overview_slide.shapes.add_shape(MSO_SHAPE.ROUNDED_RECTANGLE, right_x, card_top, right_w, card_h)
    box2.fill.solid()
    box2.fill.fore_color.rgb = RGBColor(248, 250, 252)
    box2.line.color.rgb = RGBColor(218, 225, 233)
    box2.line.width = Pt(1)

    h2 = overview_slide.shapes.add_textbox(right_x + Inches(0.25), card_top + Inches(0.18), right_w - Inches(0.5), Inches(0.35))
    h2_tf = h2.text_frame
    h2_tf.margin_top = h2_tf.margin_bottom = h2_tf.margin_left = h2_tf.margin_right = 0
    hp2 = h2_tf.paragraphs[0]
    hp2.text = "VERIFICATION CONVENTIONS & TRUTH"
    hp2.font.size = Pt(11.5)
    hp2.font.bold = True
    hp2.font.color.rgb = RGBColor(9, 105, 218)

    info_box = overview_slide.shapes.add_textbox(right_x + Inches(0.25), card_top + Inches(0.58), right_w - Inches(0.5), card_h - Inches(0.7))
    itf = info_box.text_frame
    itf.margin_top = itf.margin_bottom = itf.margin_left = itf.margin_right = 0
    itf.word_wrap = True

    ip = itf.paragraphs[0]
    ip.text = "Plot Curve Line Styles:"
    ip.font.size = Pt(11)
    ip.font.bold = True
    ip.font.color.rgb = RGBColor(30, 45, 65)

    ip = itf.add_paragraph()
    ip.text = "  ─── Solid Line : RMSE (continuous) / CSI (categorical)"
    ip.font.size = Pt(10)
    ip.font.color.rgb = RGBColor(50, 65, 85)

    ip = itf.add_paragraph()
    ip.text = "  ┈┈┈ Dashed Line : BIAS (continuous) / Freq BIAS"
    ip.font.size = Pt(10)
    ip.font.color.rgb = RGBColor(50, 65, 85)

    ip = itf.add_paragraph()
    ip.text = ""  # spacer

    ip = itf.add_paragraph()
    ip.text = "Truth & Observation Sources:"
    ip.font.size = Pt(11)
    ip.font.bold = True
    ip.font.color.rgb = RGBColor(30, 45, 65)

    obs_items = [
        ("Surface", "METAR / MADIS Surface Observations"),
        ("Upper Air", "RAOBs (Traditional Soundings)"),
        ("Radar Refl", "NOAA NSSL MRMS CREF Mosaic"),
        ("Ceiling & Vis", "METAR Observations"),
    ]
    for label, desc in obs_items:
        ip = itf.add_paragraph()
        ip.text = f"  • {label}: {desc}"
        ip.font.size = Pt(9.8)
        ip.font.color.rgb = RGBColor(50, 65, 85)


def _describe_missing_reason(error: Exception) -> str:
    if isinstance(error, FileNotFoundError):
        return "Figure missing or skipped during plotting"
    if "expected a PNG figure" in str(error):
        return "Invalid image format (expected PNG)"
    if isinstance(error, OSError):
        return "Cannot read figure file"
    return "Corrupt or unreadable PNG figure"


def _render_missing_figure_png(
    relative_path: str,
    reason: str,
    width: int = 1200,
    height: int = 900,
) -> tuple[bytes, int, int]:
    """Renders an in-memory PNG placeholder for a missing or unreadable MATS figure."""
    from PIL import Image, ImageDraw, ImageFont

    w = max(400, int(width))
    h = max(300, int(height))
    img = Image.new("RGB", (w, h), color=(244, 247, 250))
    draw = ImageDraw.Draw(img)

    pad = 18
    draw.rectangle([(pad, pad), (w - pad - 1, h - pad - 1)], outline=(185, 198, 214), width=4)

    def _font(size: int):
        try:
            return ImageFont.load_default(size=size)
        except TypeError:
            return ImageFont.load_default()

    scale = min(w / 1200.0, h / 900.0)
    font_title = _font(max(20, int(48 * scale)))
    font_name = _font(max(14, int(28 * scale)))
    font_reason = _font(max(12, int(24 * scale)))

    title_text = "Plot Unavailable"
    name_text = Path(relative_path).name
    max_text_w = w - 2 * (pad + 24)

    def _fit_text(text: str, font) -> str:
        bbox = draw.textbbox((0, 0), text, font=font)
        if bbox[2] - bbox[0] <= max_text_w:
            return text
        ellipsis = "..."
        trimmed = text
        while len(trimmed) > 8:
            trimmed = trimmed[:-4]
            cand = trimmed + ellipsis
            bbox = draw.textbbox((0, 0), cand, font=font)
            if bbox[2] - bbox[0] <= max_text_w:
                return cand
        return text

    name_text = _fit_text(name_text, font_name)
    reason_text = _fit_text(reason, font_reason)

    lines = [
        (title_text, font_title, (55, 72, 94)),
        (name_text, font_name, (95, 110, 130)),
        (reason_text, font_reason, (140, 82, 82)),
    ]
    boxes = [draw.textbbox((0, 0), text, font=font) for text, font, _ in lines]
    widths = [bbox[2] - bbox[0] for bbox in boxes]
    heights = [bbox[3] - bbox[1] for bbox in boxes]

    gap_y = max(14, int(24 * scale))
    total_h = sum(heights) + gap_y * (len(lines) - 1)
    y = (h - total_h) / 2
    for (text, font, color), bbox, tw, th in zip(lines, boxes, widths, heights):
        x = (w - tw) / 2 - bbox[0]
        draw.text((x, y - bbox[1]), text, fill=color, font=font)
        y += th + gap_y

    buf = BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue(), w, h


def build_slides(
    plan: Plan,
    output: Path,
    *,
    log=None,
    missing: list[tuple[str, str]] | None = None,
    graphics_stats: dict[str, int] | None = None,
) -> Path:
    has_mats_slides = bool(plan.slides)
    has_graphics_slides = bool(plan.pygraf_graphics and plan.pygraf_graphics.enable)

    if plan.slide_error is not None and not has_graphics_slides:
        raise SlideError(plan.slide_error)
    if not has_mats_slides and not has_graphics_slides:
        raise SlideError("No slides configured; add a slides or pygraf graphics slides section to the recipe")
    try:
        from PIL import Image
        from pptx import Presentation
        from pptx.enum.text import MSO_ANCHOR, MSO_AUTO_SIZE, PP_ALIGN
        from pptx.util import Inches, Pt
    except ImportError as error:
        raise SlideError("PowerPoint dependencies are missing; rerun AutoMATS setup") from error

    job_dimensions = {job.relative_path: (job.width, job.height) for job in plan.jobs}

    deck = Presentation()
    deck.slide_width = Inches(13 + 1 / 3)
    deck.slide_height = Inches(7.5)
    margin = Inches(0.25)
    gap = Inches(0.12)
    caption_width = Inches(0.65)
    caption_gap = Inches(0.08)
    caption_height = Inches(0.32)
    content_top = Inches(0.9)

    # 1. Create Overview Slide at index 0
    overview_slide = deck.slides.add_slide(deck.slide_layouts[6])
    section_first_slide: Dict[str, Any] = {}

    # 2. Build MATS verification slides
    for index, spec in enumerate(plan.slides, 1):
        slide = deck.slides.add_slide(deck.slide_layouts[6])

        # Track first slide for quick navigation
        sec_name = classify_slide_section(spec.title, spec.plot_group)
        if sec_name not in section_first_slide:
            section_first_slide[sec_name] = slide

        # Heading (centered with symmetric 1.6" margins to avoid backlink button overlay)
        heading_w = deck.slide_width - Inches(3.2)
        heading_left = Inches(1.6)
        heading = slide.shapes.add_textbox(heading_left, Inches(0.18), heading_w, Inches(0.55))
        heading.text_frame.word_wrap = True
        heading.text_frame.auto_size = MSO_AUTO_SIZE.TEXT_TO_FIT_SHAPE
        paragraph = heading.text_frame.paragraphs[0]
        paragraph.text = spec.title
        paragraph.alignment = PP_ALIGN.CENTER
        paragraph.font.size = Pt(24)
        paragraph.font.bold = True

        # Add top-right backlink to Overview slide
        add_overview_backlink(slide, overview_slide, deck.slide_width)

        row_height = (deck.slide_height - content_top - margin - gap * (len(spec.rows) - 1)) // len(spec.rows)
        caption_reserve = caption_width + caption_gap
        for row_number, row in enumerate(spec.rows, 1):
            cell_width = (deck.slide_width - 2 * margin - gap * (len(row) - 1)) // len(row)
            image_width = cell_width - caption_reserve
            if row_height <= 0 or image_width <= 0:
                raise SlideError(f"Slide {index}, row {row_number}: too many rows or figures to fit")
            row_top = content_top + (row_number - 1) * (row_height + gap)
            for column, relative_path in enumerate(row):
                path = output / relative_path
                try:
                    data = path.read_bytes()
                    with Image.open(BytesIO(data)) as image:
                        if image.format != "PNG":
                            raise ValueError("expected a PNG figure")
                        pixel_width, pixel_height = image.size
                        image.verify()
                except (OSError, ValueError, SyntaxError, EOFError) as error:
                    warning = (
                        f"Warning: Slide {index} ({spec.plot_group}), row {row_number}: "
                        f"cannot use {path} ({error}); using placeholder."
                    )
                    if log is not None:
                        log(warning)
                    else:
                        print(warning, file=sys.stderr)
                    if missing is not None:
                        missing.append((relative_path, str(error)))
                    ph_w, ph_h = job_dimensions.get(relative_path, (1200, 900))
                    data, pixel_width, pixel_height = _render_missing_figure_png(
                        relative_path,
                        _describe_missing_reason(error),
                        width=ph_w,
                        height=ph_h,
                    )
                scale = min(image_width / pixel_width, row_height / pixel_height)
                width = max(1, int(pixel_width * scale))
                height = max(1, int(pixel_height * scale))
                cell_left = margin + column * (cell_width + gap)
                left = max(cell_left + caption_reserve, cell_left + (cell_width - width) // 2)
                top = row_top + (row_height - height) // 2
                picture = slide.shapes.add_picture(BytesIO(data), left, top, width, height)
                picture.name = relative_path
                caption_text = spec.captions[row_number - 1][column] if spec.captions else \
                    Path(relative_path).stem[len(spec.plot_group) + 1:].split("_", 1)[0]
                label_height = min(caption_height, height)
                caption = slide.shapes.add_textbox(left - caption_width - caption_gap,
                                                  top + (height - label_height) // 2,
                                                  caption_width, label_height)
                caption.name = f"Caption {relative_path}"
                frame = caption.text_frame
                frame.margin_left = frame.margin_right = frame.margin_top = frame.margin_bottom = 0
                frame.word_wrap = False
                frame.auto_size = MSO_AUTO_SIZE.TEXT_TO_FIT_SHAPE
                frame.vertical_anchor = MSO_ANCHOR.MIDDLE
                paragraph = frame.paragraphs[0]
                paragraph.text = caption_text
                paragraph.alignment = PP_ALIGN.RIGHT
                paragraph.font.size = Pt(12)
                paragraph.font.bold = True

    # 3. Build Graphics slides if enabled
    if has_graphics_slides:
        before_count = len(deck.slides)
        from automats.graphics import process_graphics_slides
        process_graphics_slides(plan, output, deck=deck, stats=graphics_stats, log=log)
        if len(deck.slides) > before_count:
            section_first_slide["pygraf"] = deck.slides[before_count]
            for slide_idx in range(before_count, len(deck.slides)):
                add_overview_backlink(deck.slides[slide_idx], overview_slide, deck.slide_width)

    # 4. Populate Overview Slide elements and interactive hyperlinks
    populate_overview_slide(overview_slide, plan, section_first_slide)

    output.mkdir(parents=True, exist_ok=True)
    destination = output / "slides.pptx"
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=output, prefix=".slides-", suffix=".pptx", delete=False) as stream:
            temporary = Path(stream.name)
            deck.save(stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, destination)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return destination
