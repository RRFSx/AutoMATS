"""Build a local PowerPoint deck with an interactive Overview slide from the recipe's resolved PNG rows."""

from __future__ import annotations

from datetime import datetime
from io import BytesIO
import json
import os
from pathlib import Path
import re
import sys
import tempfile
from typing import Any, Dict, List, Optional, Tuple

from automats.config import Plan, PlotJob, copy_used_recipes


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


def _load_result_store_records(output: Path) -> Dict[str, dict]:
    state_path = output / ".automats-state.json"
    if not state_path.is_file():
        return {}
    try:
        data = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {}
    results = data.get("results") if isinstance(data, dict) else None
    return results if isinstance(results, dict) else {}


def _split_caption_tokens(text: str) -> List[str]:
    tokens: List[str] = []
    cur: List[str] = []
    depth = 0
    i = 0
    while i < len(text):
        ch = text[i]
        if ch == "(":
            depth += 1
            cur.append(ch)
        elif ch == ")" and depth > 0:
            depth -= 1
            cur.append(ch)
        elif ch == "," and depth == 0:
            token = "".join(cur).strip()
            if token:
                tokens.append(token)
            cur = []
        else:
            cur.append(ch)
        i += 1
    token = "".join(cur).strip()
    if token:
        tokens.append(token)
    return tokens


def _figure_caption_text(fig_meta: dict) -> Optional[str]:
    header = fig_meta.get("header")
    if not isinstance(header, list):
        return None
    parts = [str(part).strip() for part in header if str(part).strip()]
    if not parts:
        return None
    tokens: List[str] = []
    for tok in _split_caption_tokens(", ".join(parts)):
        cf = tok.casefold()
        if cf == "dieoff-type=dieoff":
            continue
        if cf in ("dieoff-type=valid time", "dieoff-type=valid_time"):
            tokens.append("Valid Time")
        else:
            tokens.append(tok)
    return ", ".join(tokens) if tokens else None


def _item_variable_tokens(record: Optional[dict], job: Optional[PlotJob]) -> set[str]:
    from automats.config import VARIABLE_ALIASES, VARIABLES

    vals: set[str] = set()
    if isinstance(record, dict):
        mats = record.get("mats") if isinstance(record.get("mats"), dict) else {}
        eff = mats.get("effective_settings") or (mats.get("figure") or {}).get("effective_settings")
        if isinstance(eff, dict):
            curves = eff.get("curves")
            if isinstance(curves, list) and curves and isinstance(curves[0], dict):
                for key in ("variable", "x-variable", "y-variable"):
                    v = str(curves[0].get(key) or "").strip()
                    if v:
                        vals.add(v.casefold())
        cfg = record.get("configuration")
        if isinstance(cfg, dict):
            for key in ("variable", "variable_tag"):
                v = str(cfg.get(key) or "").strip()
                if v:
                    vals.add(v.casefold())
    if job is not None:
        for v in (job.variable, job.variable_tag):
            if v:
                vals.add(str(v).strip().casefold())
    expanded = set(vals)
    for token in vals:
        if token in VARIABLE_ALIASES:
            expanded.add(VARIABLE_ALIASES[token].casefold())
        if token in VARIABLES:
            for item in VARIABLES[token]:
                expanded.add(item.casefold())
    return expanded


def _extract_shared_slide_caption(
    real_items: List[Dict[str, Any]],
    jobs_by_path: Dict[str, PlotJob],
    slide_title: str,
) -> Optional[str]:
    if not real_items:
        return None
    token_lists: List[List[str]] = []
    var_sets: List[set[str]] = []
    side_labels: List[str] = []

    for item in real_items:
        raw_cap = _figure_caption_text(item["fig_meta"])
        if not raw_cap:
            return None
        toks = _split_caption_tokens(raw_cap)
        if not toks:
            return None
        token_lists.append(toks)
        var_sets.append(_item_variable_tokens(item.get("record"), jobs_by_path.get(item["relative_path"])))
        side_labels.append(str(item.get("side_label") or "").strip())

    first_len = len(token_lists[0])
    if any(len(toks) != first_len for toks in token_lists[1:]):
        return None

    kept: List[str] = []
    for col_idx in range(first_len):
        col_vals = [toks[col_idx] for toks in token_lists]
        if len(set(col_vals)) == 1:
            kept.append(col_vals[0])
            continue

        # Omit variable token if each figure's variable maps cleanly to distinct per-figure side labels.
        is_var_col = all(col_vals[i].strip().casefold() in var_sets[i] for i in range(len(real_items)))
        if is_var_col and all(side_labels):
            label_vars: Dict[str, set[str]] = {}
            for lbl, val in zip(side_labels, col_vals):
                label_vars.setdefault(lbl.casefold(), set()).add(val.strip().casefold())
            if all(len(vs) == 1 for vs in label_vars.values()) and len(label_vars) >= len({v.strip().casefold() for v in col_vals}):
                continue

        # Omit lead token (e.g. f003h) if each distinct lead is already shown in the slide title or side label.
        is_lead_col = all(re.fullmatch(r"f\d+h?", val.strip(), re.IGNORECASE) for val in col_vals)
        if is_lead_col:
            leads_visible = True
            for val, lbl in zip(col_vals, side_labels):
                stem = val.strip()[:-1] if val.strip().lower().endswith("h") else val.strip()
                pat = re.compile(rf"(?<![a-z0-9]){re.escape(stem)}h?(?![0-9])", re.IGNORECASE)
                if not (pat.search(slide_title or "") or pat.search(lbl)):
                    leads_visible = False
                    break
            if leads_visible:
                continue

        return None

    return ", ".join(kept) if kept else None


def _resolve_figure_legend_items(
    record: dict,
    job: Optional[PlotJob],
) -> Optional[Tuple[List[Dict[str, str]], Tuple[str, ...]]]:
    mats = record.get("mats") if isinstance(record, dict) else None
    if not isinstance(mats, dict):
        return None
    fig_meta = mats.get("figure")
    if not isinstance(fig_meta, dict):
        return None
    styles = tuple(str(s).strip() for s in (fig_meta.get("legend_styles") or ("solid",)) if str(s).strip())
    if not styles:
        styles = ("solid",)

    stored_items = fig_meta.get("legend_items")
    if isinstance(stored_items, list) and stored_items:
        parsed_items: List[Dict[str, str]] = []
        valid = True
        for entry in stored_items:
            if not isinstance(entry, dict) or not entry.get("name") or not entry.get("color"):
                valid = False
                break
            parsed_items.append({"name": str(entry["name"]), "color": str(entry["color"])})
        if valid and parsed_items:
            return parsed_items, styles

    legend_names = fig_meta.get("legend_names")
    if not isinstance(legend_names, list) or not legend_names:
        return None

    candidate_jobs: List[PlotJob] = []
    cfg = record.get("configuration") if isinstance(record, dict) else None
    if isinstance(cfg, dict) and isinstance(cfg.get("experiments"), (list, tuple)):
        from automats.config import Experiment

        try:
            exps = tuple(
                Experiment(
                    name=str(e["name"]),
                    color=str(e["color"]),
                    short_name=e.get("short_name"),
                )
                for e in cfg["experiments"]
                if isinstance(e, dict) and e.get("name") and e.get("color")
            )
            if exps:
                raw_lead = cfg.get("lead")
                lead = tuple(raw_lead) if isinstance(raw_lead, (list, tuple)) else raw_lead
                raw_params = cfg.get("parameters", ())
                params = tuple((str(p[0]), str(p[1])) for p in raw_params if isinstance(p, (list, tuple)) and len(p) >= 2)
                raw_forecast_colors = cfg.get("forecast_lead_colors", ())
                fc_colors = tuple((int(c[0]), str(c[1])) for c in raw_forecast_colors if isinstance(c, (list, tuple)) and len(c) >= 2)
                cfg_job = PlotJob(
                    name=str(cfg.get("name", "")),
                    plot_type=str(cfg.get("plot_type", "")),
                    truth=str(cfg.get("truth", "")),
                    region=str(cfg.get("region", "")),
                    matched_samples=bool(cfg.get("matched_samples", True)),
                    variable=str(cfg.get("variable", "")),
                    variable_tag=str(cfg.get("variable_tag", "")),
                    statistic=str(cfg.get("statistic", "")),
                    lead=lead,
                    start=cfg.get("start"),
                    end=cfg.get("end"),
                    parameters=params,
                    experiments=exps,
                    relative_path=str(cfg.get("relative_path", "")),
                    application=str(cfg.get("application", "")),
                    forecast_lead_colors=fc_colors,
                )
                candidate_jobs.append(cfg_job)
        except (KeyError, TypeError, ValueError):
            pass
    if job is not None:
        candidate_jobs.append(job)

    from automats.browser import resolve_diff_entries

    plotted_curves = mats.get("curves")
    expected_names = [str(n) for n in legend_names]
    for candidate in candidate_jobs:
        primary = list(candidate.legend_entries)
        if isinstance(plotted_curves, list) and len(plotted_curves) < len(candidate.curves):
            plotted_names = {c.get("name") for c in plotted_curves if isinstance(c, dict) and c.get("name")}
            if len(candidate.leads) == 1:
                filtered = [entry for entry, exp in zip(candidate.legend_entries, candidate.experiments) if exp.name in plotted_names]
            else:
                filtered = [
                    entry for entry, (exp, lead_val) in zip(
                        candidate.legend_entries,
                        [(e, l) for e in candidate.experiments for l in candidate.leads],
                    )
                    if any(isinstance(c, dict) and c.get("name") == exp.name and c.get("lead") == lead_val for c in plotted_curves)
                ]
            if filtered:
                primary = filtered

        diff_entries = resolve_diff_entries(candidate, mats.get("plot_params"))
        combined = [{"name": entry.name, "color": entry.color} for entry in primary] + [
            {"name": str(d["name"]), "color": str(d["color"])} for d in diff_entries
            if isinstance(d, dict) and d.get("name") and d.get("color")
        ]
        if [item["name"] for item in combined] == expected_names:
            return combined, styles
    return None


def _find_header_split_bounds(
    raw_bytes: bytes,
    pixel_width: int,
    pixel_height: int,
    fig_meta: dict,
) -> Optional[Tuple[int, int, int]]:
    """Returns (desc_keep_bottom, legend_keep_top, header_height) or None if split is unsafe."""
    from PIL import Image

    hh = fig_meta.get("header_height")
    if type(hh) is not int or hh <= 0 or hh >= pixel_height:
        return None

    meta_desc_h = fig_meta.get("description_height")
    meta_legend_top = fig_meta.get("legend_top")
    if (
        type(meta_desc_h) is int and type(meta_legend_top) is int
        and 0 < meta_desc_h <= meta_legend_top < hh
    ):
        desc_keep_bottom = min(meta_legend_top, meta_desc_h + 2)
        legend_keep_top = max(meta_desc_h, meta_legend_top - 4)
        return desc_keep_bottom, legend_keep_top, hh

    desc_px = int(fig_meta.get("description_font_size") or 24)
    leg_px = int(fig_meta.get("legend_font_size") or 28)
    lrows = max(1, int(fig_meta.get("legend_rows") or 1))
    bottom_pad = max(6, round(leg_px * 0.25))
    leg_line_h = round(leg_px * 1.36)
    diff_extra = max(4, round(leg_px * 0.2)) if (fig_meta.get("diff_legend_names") and lrows >= 2) else 0
    est_legend_top = hh - bottom_pad - lrows * leg_line_h - diff_extra

    try:
        with Image.open(BytesIO(raw_bytes)) as img:
            rgb_bytes = img.crop((0, 0, pixel_width, hh)).convert("RGB").tobytes()
    except (OSError, ValueError):
        return None

    stride = pixel_width * 3
    row_white = [min(rgb_bytes[y * stride:(y + 1) * stride]) >= 245 for y in range(hh)]
    if not (0 < est_legend_top < hh and row_white[est_legend_top]):
        white_gaps = []
        start = None
        for y, is_white in enumerate(row_white):
            if is_white and start is None:
                start = y
            elif not is_white and start is not None:
                if start > 0 and y < hh:
                    white_gaps.append((start, y - 1))
                start = None
        if not white_gaps:
            return None
        white_start, white_end = min(
            white_gaps,
            key=lambda g: (abs(((g[0] + g[1]) / 2) - est_legend_top), -(g[1] - g[0])),
        )
    else:
        white_start = est_legend_top
        while white_start > 0 and row_white[white_start - 1]:
            white_start -= 1
        white_end = est_legend_top
        while white_end + 1 < hh and row_white[white_end + 1]:
            white_end += 1

    if white_start <= 0 or white_end >= hh - 1:
        return None
    desc_pad = max(6, round(desc_px * 0.25))
    leg_pad = max(6, round(leg_px * 0.22))
    desc_keep_bottom = min(white_end, white_start + desc_pad)
    legend_keep_top = max(white_start, white_end - leg_pad)
    return desc_keep_bottom, legend_keep_top, hh


def _prepare_slide_png(
    raw_bytes: bytes,
    pixel_width: int,
    pixel_height: int,
    fig_meta: Optional[dict],
    *,
    drop_caption: bool,
    drop_legend: bool,
) -> Tuple[bytes, int, int]:
    from PIL import Image

    if (not drop_caption and not drop_legend) or not isinstance(fig_meta, dict):
        return raw_bytes, pixel_width, pixel_height
    if not fig_meta.get("legend_in_header"):
        return raw_bytes, pixel_width, pixel_height

    hh = fig_meta.get("header_height")
    if type(hh) is not int or hh <= 0 or hh >= pixel_height:
        return raw_bytes, pixel_width, pixel_height

    if drop_caption and drop_legend:
        try:
            with Image.open(BytesIO(raw_bytes)) as img:
                cropped = img.crop((0, hh, pixel_width, pixel_height))
                buf = BytesIO()
                cropped.save(buf, format="PNG")
                return buf.getvalue(), pixel_width, pixel_height - hh
        except (OSError, ValueError):
            return raw_bytes, pixel_width, pixel_height

    bounds = _find_header_split_bounds(raw_bytes, pixel_width, pixel_height, fig_meta)
    if bounds is None:
        return raw_bytes, pixel_width, pixel_height
    desc_keep_bottom, legend_keep_top, hh = bounds

    try:
        with Image.open(BytesIO(raw_bytes)) as img:
            rgb = img.convert("RGB") if img.mode not in ("RGB", "RGBA") else img
            chart_h = pixel_height - hh
            if not drop_caption and drop_legend:
                out_h = desc_keep_bottom + chart_h
                canvas = Image.new(rgb.mode, (pixel_width, out_h), "white")
                canvas.paste(rgb.crop((0, 0, pixel_width, desc_keep_bottom)), (0, 0))
                canvas.paste(rgb.crop((0, hh, pixel_width, pixel_height)), (0, desc_keep_bottom))
            else:
                # drop_caption and not drop_legend
                top_crop = rgb.crop((0, legend_keep_top, pixel_width, pixel_height))
                buf = BytesIO()
                top_crop.save(buf, format="PNG")
                return buf.getvalue(), pixel_width, pixel_height - legend_keep_top
            buf = BytesIO()
            canvas.save(buf, format="PNG")
            return buf.getvalue(), pixel_width, out_h
    except (OSError, ValueError):
        return raw_bytes, pixel_width, pixel_height


def _estimate_text_width_inches(text: str, font_pt: float) -> float:
    wide = set("WMmw@%&")
    narrow = set("iljtI1 .,|-:/()[]'\"")
    units = 0.0
    for ch in text:
        if ch in wide:
            units += 0.85
        elif ch in narrow:
            units += 0.33
        elif ch.isupper():
            units += 0.70
        elif ch.isdigit():
            units += 0.56
        elif ch.islower():
            units += 0.53
        else:
            units += 0.55
    return max(0.10, units * (font_pt / 72.0) * 1.03 + 0.025)


def _find_free_rectangles(
    container: Tuple[int, int, int, int],
    blocked: List[Tuple[int, int, int, int]],
) -> List[Tuple[int, int, int, int]]:
    x0, y0, x1, y1 = container
    if x1 <= x0 or y1 <= y0:
        return []
    clipped = []
    for bx0, by0, bx1, by1 in blocked:
        cx0, cy0 = max(x0, bx0), max(y0, by0)
        cx1, cy1 = min(x1, bx1), min(y1, by1)
        if cx1 > cx0 and cy1 > cy0:
            clipped.append((cx0, cy0, cx1, cy1))

    xs = sorted({x0, x1} | {b[0] for b in clipped} | {b[2] for b in clipped})
    ys = sorted({y0, y1} | {b[1] for b in clipped} | {b[3] for b in clipped})
    nx = len(xs) - 1
    ny = len(ys) - 1
    grid = [[False] * nx for _ in range(ny)]
    for r in range(ny):
        ry0, ry1 = ys[r], ys[r + 1]
        for c in range(nx):
            rx0, rx1 = xs[c], xs[c + 1]
            if any(b[0] <= rx0 and rx1 <= b[2] and b[1] <= ry0 and ry1 <= b[3] for b in clipped):
                grid[r][c] = True

    candidates: List[Tuple[int, int, int, int]] = []
    for r0 in range(ny):
        col_free = [not grid[r0][c] for c in range(nx)]
        for r1 in range(r0, ny):
            if r1 > r0:
                for c in range(nx):
                    if grid[r1][c]:
                        col_free[c] = False
            c = 0
            while c < nx:
                if col_free[c]:
                    c_end = c
                    while c_end + 1 < nx and col_free[c_end + 1]:
                        c_end += 1
                    candidates.append((xs[c], ys[r0], xs[c_end + 1], ys[r1 + 1]))
                    c = c_end + 1
                else:
                    c += 1

    # Deduplicate and discard strictly-contained rectangles
    unique = list(dict.fromkeys(candidates))
    maximal = [
        rect for rect in unique
        if not any(
            other is not rect
            and other[0] <= rect[0] and other[1] <= rect[1]
            and other[2] >= rect[2] and other[3] >= rect[3]
            for other in unique
        )
    ]
    return maximal


def _layout_legend_box(
    items: List[Dict[str, str]],
    rect: Tuple[int, int, int, int],
    *,
    prefer_vertical: bool,
    font_candidates: Tuple[float, ...] = (9.5, 9.0, 8.5, 8.0),
) -> Optional[Dict[str, Any]]:
    from pptx.util import Inches

    rx0, ry0, rx1, ry1 = rect
    avail_w = rx1 - rx0
    avail_h = ry1 - ry0
    if not items or avail_w <= 0 or avail_h <= 0:
        return None

    for font_pt in font_candidates:
        scale = font_pt / 10.0
        swatch_w = Inches(0.20 * scale)
        swatch_gap = Inches(0.05 * scale)
        item_gap = Inches(0.12 * scale)
        row_h = Inches(0.18 * max(0.85, scale))
        row_gap = Inches(0.025 * max(0.8, scale))
        pad_x = Inches(0.08)
        pad_y = Inches(0.05 if (prefer_vertical and avail_h >= Inches(0.40)) else 0.035)

        label_widths = [Inches(_estimate_text_width_inches(item["name"], font_pt)) for item in items]
        entry_widths = [swatch_w + swatch_gap + lw for lw in label_widths]

        def _try_grid(num_cols: int) -> Optional[Dict[str, Any]]:
            num_cols = max(1, min(num_cols, len(items)))
            num_rows = (len(items) + num_cols - 1) // num_cols
            col_widths = [0] * num_cols
            for idx, ew in enumerate(entry_widths):
                c = idx % num_cols
                col_widths[c] = max(col_widths[c], ew)
            content_w = sum(col_widths) + item_gap * (num_cols - 1)
            content_h = num_rows * row_h + (num_rows - 1) * row_gap
            box_w = content_w + 2 * pad_x
            box_h = content_h + 2 * pad_y
            if box_w > avail_w or box_h > avail_h:
                return None
            placements = []
            for idx, lw in enumerate(label_widths):
                r = idx // num_cols
                c = idx % num_cols
                rel_x = pad_x + sum(col_widths[:c]) + c * item_gap
                rel_y = pad_y + r * (row_h + row_gap)
                placements.append((idx, rel_x, rel_y, lw))
            return {
                "width": box_w,
                "height": box_h,
                "font_pt": font_pt,
                "swatch_w": swatch_w,
                "swatch_gap": swatch_gap,
                "row_h": row_h,
                "placements": placements,
            }

        def _try_flow() -> Optional[Dict[str, Any]]:
            max_content_w = avail_w - 2 * pad_x
            if max_content_w <= 0 or any(ew > max_content_w for ew in entry_widths):
                return None
            rows_items: List[List[int]] = []
            row_widths: List[int] = []
            cur_row: List[int] = []
            cur_w = 0
            for idx, ew in enumerate(entry_widths):
                needed = ew if not cur_row else cur_w + item_gap + ew
                if cur_row and needed > max_content_w:
                    rows_items.append(cur_row)
                    row_widths.append(cur_w)
                    cur_row = [idx]
                    cur_w = ew
                else:
                    cur_row.append(idx)
                    cur_w = needed
            if cur_row:
                rows_items.append(cur_row)
                row_widths.append(cur_w)
            num_rows = len(rows_items)
            box_w = max(row_widths) + 2 * pad_x
            box_h = num_rows * row_h + (num_rows - 1) * row_gap + 2 * pad_y
            if box_w > avail_w or box_h > avail_h:
                return None
            placements = []
            for r, r_indices in enumerate(rows_items):
                cur_x = pad_x
                rel_y = pad_y + r * (row_h + row_gap)
                for idx in r_indices:
                    placements.append((idx, cur_x, rel_y, label_widths[idx]))
                    cur_x += entry_widths[idx] + item_gap
            return {
                "width": box_w,
                "height": box_h,
                "font_pt": font_pt,
                "swatch_w": swatch_w,
                "swatch_gap": swatch_gap,
                "row_h": row_h,
                "placements": placements,
            }

        if prefer_vertical and avail_h >= Inches(0.40):
            for candidate in (_try_grid(1), _try_grid(2) if len(items) >= 4 else None, _try_flow()):
                if candidate is not None:
                    return candidate
        else:
            for candidate in (_try_flow(), _try_grid(1)):
                if candidate is not None:
                    return candidate
    return None


def _place_legend_box_in_rect(
    layout: Dict[str, Any],
    rect: Tuple[int, int, int, int],
    slide_w: int,
    content_top: int,
    grid_bottom: int,
    *,
    goal_xy: Optional[Tuple[float, float]] = None,
) -> Tuple[int, int]:
    from pptx.util import Inches

    rx0, ry0, rx1, ry1 = rect
    box_w = layout["width"]
    box_h = layout["height"]
    avail_w = rx1 - rx0
    avail_h = ry1 - ry0

    if goal_xy is not None:
        goal_x = int(goal_xy[0])
        goal_y = int(goal_xy[1])
    else:
        goal_x = int(slide_w * 0.78)
        goal_y = int(content_top + (grid_bottom - content_top) * 0.75)

    if avail_w <= box_w + Inches(2.5):
        box_left = rx0 + (avail_w - box_w) // 2
    else:
        box_left = max(rx0, min(rx1 - box_w, goal_x - box_w // 2))

    if avail_h <= box_h + Inches(2.5):
        box_top = ry0 + (avail_h - box_h) // 2
    else:
        box_top = max(ry0, min(ry1 - box_h, goal_y - box_h // 2))

    return box_left, box_top


def _choose_free_space_legend(
    items: List[Dict[str, str]],
    occupied_boxes: List[Tuple[int, int, int, int]],
    slide_w: int,
    margin: int,
    content_top: int,
    grid_bottom: int,
    *,
    target_xy: Optional[Tuple[float, float]] = None,
) -> Optional[Dict[str, Any]]:
    from pptx.util import Inches

    free_rects = _find_free_rectangles(
        (margin, content_top, slide_w - margin, grid_bottom),
        occupied_boxes,
    )
    mid_y = (content_top + grid_bottom) / 2
    goal_x = target_xy[0] if target_xy is not None else slide_w * 0.78
    goal_y = target_xy[1] if target_xy is not None else content_top + (grid_bottom - content_top) * 0.75

    best: Optional[Tuple[Tuple[int, float, float], Dict[str, Any]]] = None
    for rect in free_rects:
        rx0, ry0, rx1, ry1 = rect
        if rx1 - rx0 < Inches(0.70) or ry1 - ry0 < Inches(0.30):
            continue
        layout = _layout_legend_box(items, rect, prefer_vertical=True)
        if layout is None:
            continue
        box_left, box_top = _place_legend_box_in_rect(
            layout,
            rect,
            slide_w,
            content_top,
            grid_bottom,
            goal_xy=(goal_x, goal_y),
        )
        cx = box_left + layout["width"] / 2
        cy = box_top + layout["height"] / 2
        if target_xy is not None:
            contains_target = 0 if (rx0 <= goal_x <= rx1 and ry0 <= goal_y <= ry1) else 1
            norm_dist = ((cx - goal_x) / max(1, slide_w)) ** 2 + ((cy - goal_y) / max(1, grid_bottom - content_top)) ** 2
            score = (contains_target, norm_dist, -layout["font_pt"])
        else:
            in_bottom_right = 0 if (cx >= slide_w / 2 and cy >= mid_y) else (1 if cy >= mid_y or cx >= slide_w / 2 else 2)
            norm_dist = ((cx - goal_x) / max(1, slide_w)) ** 2 + ((cy - goal_y) / max(1, grid_bottom - content_top)) ** 2
            score = (in_bottom_right, norm_dist, -layout["font_pt"])
        candidate = {"left": box_left, "top": box_top, "layout": layout}
        if best is None or score < best[0]:
            best = (score, candidate)
    return best[1] if best is not None else None


def _draw_shared_legend_box(slide: Any, items: List[Dict[str, str]], placement: Dict[str, Any]) -> None:
    from pptx.dml.color import RGBColor
    from pptx.enum.shapes import MSO_CONNECTOR, MSO_SHAPE
    from pptx.enum.text import MSO_ANCHOR, PP_ALIGN
    from pptx.util import Inches, Pt

    box_left = placement["left"]
    box_top = placement["top"]
    layout = placement["layout"]
    box_w = layout["width"]
    box_h = layout["height"]
    font_pt = layout["font_pt"]
    swatch_w = layout["swatch_w"]
    swatch_gap = layout["swatch_gap"]
    row_h = layout["row_h"]

    card = slide.shapes.add_shape(MSO_SHAPE.ROUNDED_RECTANGLE, box_left, box_top, box_w, box_h)
    card.name = "Slide Shared Legend"
    try:
        card.adjustments[0] = min(0.25, max(0.05, (Inches(0.06) / max(1, min(box_w, box_h)))))
    except (IndexError, AttributeError):
        pass
    card.fill.solid()
    card.fill.fore_color.rgb = RGBColor(248, 250, 252)
    card.line.color.rgb = RGBColor(210, 220, 232)
    card.line.width = Pt(0.75)

    dot_size = Inches(0.058 * max(0.85, font_pt / 10.0))
    for idx, rel_x, rel_y, label_w in layout["placements"]:
        item = items[idx]
        c_rgb = parse_color_to_rgb(item["color"])
        sx0 = box_left + rel_x
        sx1 = sx0 + swatch_w
        mid_y = box_top + rel_y + row_h // 2

        line = slide.shapes.add_connector(MSO_CONNECTOR.STRAIGHT, sx0, mid_y, sx1, mid_y)
        line.name = f"Legend Swatch {item['name']}"
        line.line.color.rgb = c_rgb
        line.line.width = Pt(1.8)

        dot = slide.shapes.add_shape(
            MSO_SHAPE.OVAL,
            (sx0 + sx1) // 2 - dot_size // 2,
            mid_y - dot_size // 2,
            dot_size,
            dot_size,
        )
        dot.name = f"Legend Marker {item['name']}"
        dot.fill.solid()
        dot.fill.fore_color.rgb = c_rgb
        dot.line.fill.background()

        label = slide.shapes.add_textbox(
            sx1 + swatch_gap,
            box_top + rel_y,
            label_w,
            row_h,
        )
        label.name = f"Legend Label {item['name']}"
        ltf = label.text_frame
        ltf.margin_left = ltf.margin_right = ltf.margin_top = ltf.margin_bottom = 0
        ltf.word_wrap = False
        ltf.vertical_anchor = MSO_ANCHOR.MIDDLE
        lp = ltf.paragraphs[0]
        lp.text = item["name"]
        lp.alignment = PP_ALIGN.LEFT
        lp.font.size = Pt(font_pt)
        lp.font.bold = True
        lp.font.color.rgb = RGBColor(32, 32, 32)


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
        from pptx.dml.color import RGBColor
        from pptx.enum.text import MSO_ANCHOR, MSO_AUTO_SIZE, PP_ALIGN
        from pptx.util import Inches, Pt
    except ImportError as error:
        raise SlideError("PowerPoint dependencies are missing; rerun AutoMATS setup") from error

    job_dimensions = {job.relative_path: (job.width, job.height) for job in plan.jobs}
    jobs_by_path = {job.relative_path: job for job in plan.jobs}
    store_records = _load_result_store_records(output)

    deck = Presentation()
    deck.slide_width = Inches(13 + 1 / 3)
    deck.slide_height = Inches(7.5)
    margin = Inches(0.25)
    gap = Inches(0.12)
    caption_width = Inches(0.65)
    caption_gap = Inches(0.08)
    caption_height = Inches(0.32)
    content_top = Inches(0.9)
    caption_reserve = caption_width + caption_gap

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

        # Load all figures on this slide once and inspect metadata for caption/legend deduplication
        loaded_rows: List[List[Dict[str, Any]]] = []
        real_records: List[dict] = []
        all_real_have_header_meta = True
        for row_number, row in enumerate(spec.rows, 1):
            row_items: List[Dict[str, Any]] = []
            for column, relative_path in enumerate(row):
                path = output / relative_path
                is_placeholder = False
                try:
                    data = path.read_bytes()
                    with Image.open(BytesIO(data)) as image:
                        if image.format != "PNG":
                            raise ValueError("expected a PNG figure")
                        pixel_width, pixel_height = image.size
                        image.verify()
                except (OSError, ValueError, SyntaxError, EOFError) as error:
                    is_placeholder = True
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

                record = None if is_placeholder else store_records.get(relative_path)
                fig_meta = None
                if not is_placeholder:
                    if isinstance(record, dict):
                        fm = record.get("mats", {}).get("figure") if isinstance(record.get("mats"), dict) else None
                        if isinstance(fm, dict) and fm.get("legend_in_header") and type(fm.get("header_height")) is int and fm["header_height"] > 0:
                            fig_meta = fm
                            real_records.append(record)
                        else:
                            all_real_have_header_meta = False
                    else:
                        all_real_have_header_meta = False
                side_label = spec.captions[row_number - 1][column] if spec.captions else \
                    Path(relative_path).stem[len(spec.plot_group) + 1:].split("_", 1)[0]
                row_items.append({
                    "relative_path": relative_path,
                    "side_label": side_label,
                    "data": data,
                    "pixel_width": pixel_width,
                    "pixel_height": pixel_height,
                    "is_placeholder": is_placeholder,
                    "record": record,
                    "fig_meta": fig_meta,
                })
            loaded_rows.append(row_items)

        shared_caption: Optional[str] = None
        shared_legend_items: Optional[List[Dict[str, str]]] = None
        if all_real_have_header_meta and real_records:
            real_items = [it for r in loaded_rows for it in r if not it["is_placeholder"]]
            shared_caption = _extract_shared_slide_caption(real_items, jobs_by_path, spec.title)

            legend_keys = []
            resolved_legend_pairs = []
            for item in real_items:
                resolved = _resolve_figure_legend_items(item["record"], jobs_by_path.get(item["relative_path"]))
                if resolved is None:
                    resolved_legend_pairs = []
                    break
                items_list, styles = resolved
                key = (
                    tuple((entry["name"], tuple(parse_color_to_rgb(entry["color"]))) for entry in items_list),
                    styles,
                )
                legend_keys.append(key)
                resolved_legend_pairs.append(items_list)
            if resolved_legend_pairs and len(set(legend_keys)) == 1:
                shared_legend_items = resolved_legend_pairs[0]

        drop_caption = shared_caption is not None
        drop_legend = shared_legend_items is not None
        if drop_caption or drop_legend:
            for row_items in loaded_rows:
                for item in row_items:
                    if not item["is_placeholder"]:
                        new_data, new_w, new_h = _prepare_slide_png(
                            item["data"],
                            item["pixel_width"],
                            item["pixel_height"],
                            item["fig_meta"],
                            drop_caption=drop_caption,
                            drop_legend=drop_legend,
                        )
                        item["data"] = new_data
                        item["pixel_width"] = new_w
                        item["pixel_height"] = new_h

        # Heading (centered with symmetric 1.6" margins to avoid backlink button overlay)
        heading_w = deck.slide_width - Inches(3.2)
        heading_left = Inches(1.6)
        heading_top = Inches(0.10) if shared_caption else Inches(0.18)
        heading_h = Inches(0.45) if shared_caption else Inches(0.55)
        heading = slide.shapes.add_textbox(heading_left, heading_top, heading_w, heading_h)
        heading.text_frame.word_wrap = True
        heading.text_frame.auto_size = MSO_AUTO_SIZE.TEXT_TO_FIT_SHAPE
        if shared_caption:
            heading.text_frame.margin_top = heading.text_frame.margin_bottom = 0
            heading.text_frame.vertical_anchor = MSO_ANCHOR.MIDDLE
        paragraph = heading.text_frame.paragraphs[0]
        paragraph.text = spec.title
        paragraph.alignment = PP_ALIGN.CENTER
        paragraph.font.size = Pt(24)
        paragraph.font.bold = True

        if shared_caption:
            cap_w = deck.slide_width - Inches(0.70)
            cap_box = slide.shapes.add_textbox(
                Inches(0.35),
                Inches(0.55),
                cap_w,
                Inches(0.28),
            )
            cap_box.name = "Slide Shared Caption"
            ctf = cap_box.text_frame
            ctf.margin_left = ctf.margin_right = ctf.margin_top = ctf.margin_bottom = 0
            ctf.word_wrap = False
            ctf.auto_size = MSO_AUTO_SIZE.TEXT_TO_FIT_SHAPE
            ctf.vertical_anchor = MSO_ANCHOR.MIDDLE
            cap_font_pt = 11.5
            est_cap_w = _estimate_text_width_inches(shared_caption, cap_font_pt)
            avail_cap_w = cap_w / Inches(1) - 0.1
            if est_cap_w > avail_cap_w:
                cap_font_pt = max(8.5, round(cap_font_pt * (avail_cap_w / est_cap_w), 1))
            cp = ctf.paragraphs[0]
            cp.text = shared_caption
            cp.alignment = PP_ALIGN.CENTER
            cp.font.size = Pt(cap_font_pt)
            cp.font.color.rgb = RGBColor(45, 55, 70)

        # Add top-right backlink to Overview slide
        add_overview_backlink(slide, overview_slide, deck.slide_width)

        def _compute_grid_placements(
            grid_bottom: int,
            *,
            split_col: Optional[int] = None,
            target_col_gap: int = 0,
        ) -> Optional[Tuple[List[Dict[str, Any]], List[Tuple[int, int, int, int]]]]:
            row_height = (grid_bottom - content_top - gap * (len(spec.rows) - 1)) // len(spec.rows)
            placements_out: List[Dict[str, Any]] = []
            occupied_out: List[Tuple[int, int, int, int]] = []
            pad_emu = Inches(0.06)

            if (
                split_col is not None
                and target_col_gap > 0
                and len({len(r) for r in loaded_rows}) == 1
                and len(loaded_rows[0]) >= 2
            ):
                num_cols = len(loaded_rows[0])
                outer_span = (deck.slide_width - margin) - (margin + caption_reserve)
                other_count = num_cols - 2
                min_other = gap + caption_reserve
                mid_gap = max(gap + caption_reserve, target_col_gap)
                plot_budget = outer_span - other_count * min_other - mid_gap
                if row_height <= 0 or plot_budget < num_cols * Inches(1.5):
                    return None
                image_width = plot_budget // num_cols
                row_sized: List[List[Tuple[int, int, Dict[str, Any]]]] = []
                col_max_w = [0] * num_cols
                for row_items in loaded_rows:
                    sized_row = []
                    for col_idx, item in enumerate(row_items):
                        pixel_width = item["pixel_width"]
                        pixel_height = item["pixel_height"]
                        scale = min(image_width / pixel_width, row_height / pixel_height)
                        w = max(1, int(pixel_width * scale))
                        h = max(1, int(pixel_height * scale))
                        col_max_w[col_idx] = max(col_max_w[col_idx], w)
                        sized_row.append((w, h, item))
                    row_sized.append(sized_row)
                slack = outer_span - sum(col_max_w)
                if other_count > 0:
                    extra = slack - other_count * min_other - mid_gap
                    each_other = min_other + extra // (other_count + 1)
                    actual_mid_gap = slack - other_count * each_other
                else:
                    each_other = 0
                    actual_mid_gap = slack
                col_starts: List[int] = []
                x_cur = margin + caption_reserve
                for col_idx in range(num_cols):
                    col_starts.append(x_cur)
                    x_cur += col_max_w[col_idx] + (actual_mid_gap if col_idx == split_col else each_other)

                for row_number, sized_row in enumerate(row_sized, 1):
                    row_top = content_top + (row_number - 1) * (row_height + gap)
                    for column, (width, height, item) in enumerate(sized_row):
                        left = col_starts[column] + (col_max_w[column] - width) // 2
                        top = row_top + (row_height - height) // 2
                        caption_text = item["side_label"]
                        label_height = min(caption_height, height)
                        cap_top = top + (height - label_height) // 2
                        placements_out.append({
                            "item": item,
                            "col": column,
                            "left": left,
                            "top": top,
                            "width": width,
                            "height": height,
                            "caption_text": caption_text,
                            "label_height": label_height,
                        })
                        occupied_out.append((left - pad_emu, top - pad_emu, left + width + pad_emu, top + height + pad_emu))
                        occupied_out.append((
                            left - caption_width - caption_gap - pad_emu,
                            cap_top - pad_emu,
                            left - caption_gap + pad_emu,
                            cap_top + label_height + pad_emu,
                        ))
                return placements_out, occupied_out

            for row_number, row_items in enumerate(loaded_rows, 1):
                cell_width = (deck.slide_width - 2 * margin - gap * (len(row_items) - 1)) // len(row_items)
                image_width = cell_width - caption_reserve
                if row_height <= 0 or image_width <= 0:
                    raise SlideError(f"Slide {index}, row {row_number}: too many rows or figures to fit")
                row_top = content_top + (row_number - 1) * (row_height + gap)
                for column, item in enumerate(row_items):
                    pixel_width = item["pixel_width"]
                    pixel_height = item["pixel_height"]
                    scale = min(image_width / pixel_width, row_height / pixel_height)
                    width = max(1, int(pixel_width * scale))
                    height = max(1, int(pixel_height * scale))
                    cell_left = margin + column * (cell_width + gap)
                    left = max(cell_left + caption_reserve, cell_left + (cell_width - width) // 2)
                    top = row_top + (row_height - height) // 2
                    caption_text = item["side_label"]
                    label_height = min(caption_height, height)
                    cap_top = top + (height - label_height) // 2
                    placements_out.append({
                        "item": item,
                        "col": column,
                        "left": left,
                        "top": top,
                        "width": width,
                        "height": height,
                        "caption_text": caption_text,
                        "label_height": label_height,
                    })
                    occupied_out.append((left - pad_emu, top - pad_emu, left + width + pad_emu, top + height + pad_emu))
                    occupied_out.append((
                        left - caption_width - caption_gap - pad_emu,
                        cap_top - pad_emu,
                        left - caption_gap + pad_emu,
                        cap_top + label_height + pad_emu,
                    ))
            return placements_out, occupied_out

        full_bottom = deck.slide_height - margin
        figure_placements, occupied_boxes = _compute_grid_placements(full_bottom)

        legend_placement: Optional[Dict[str, Any]] = None
        if shared_legend_items:
            row_counts = [len(r) for r in loaded_rows]
            is_uniform_multi_col = len(row_counts) >= 2 and len(set(row_counts)) == 1 and row_counts[0] >= 2
            # If the slide is a uniform multi-column, multi-row grid (2x2 -> col gap 0; 2x3 -> col gap 1),
            # prefer placing the legend in the middle of that target empty column.
            if is_uniform_multi_col:
                split_col = 0 if row_counts[0] == 2 else 1
                gap_side_margin = Inches(0.14)
                grid_mid_y = (content_top + full_bottom) / 2
                left_col_right = max(p["left"] + p["width"] for p in figure_placements if p["col"] == split_col)
                right_col_left = min(p["left"] for p in figure_placements if p["col"] == split_col + 1)
                gap_cx = (left_col_right + right_col_left) / 2
                candidate = _choose_free_space_legend(
                    shared_legend_items,
                    occupied_boxes,
                    deck.slide_width,
                    margin,
                    content_top,
                    full_bottom,
                    target_xy=(gap_cx, grid_mid_y),
                )
                fits_column_gap = (
                    candidate is not None
                    and candidate["left"] >= left_col_right + gap_side_margin
                    and candidate["left"] + candidate["layout"]["width"] <= right_col_left - gap_side_margin
                )
                if not fits_column_gap:
                    probe = _layout_legend_box(
                        shared_legend_items,
                        (0, 0, Inches(2.2), Inches(2.6)),
                        prefer_vertical=True,
                    )
                    if probe is not None:
                        needed_gap = max(probe["width"] + 2 * gap_side_margin, gap + caption_reserve)
                        widened = _compute_grid_placements(
                            full_bottom,
                            split_col=split_col,
                            target_col_gap=needed_gap,
                        )
                        if widened is not None:
                            w_places, w_occupied = widened
                            w_left_r = max(p["left"] + p["width"] for p in w_places if p["col"] == split_col)
                            w_right_l = min(p["left"] for p in w_places if p["col"] == split_col + 1)
                            w_candidate = _choose_free_space_legend(
                                shared_legend_items,
                                w_occupied,
                                deck.slide_width,
                                margin,
                                content_top,
                                full_bottom,
                                target_xy=((w_left_r + w_right_l) / 2, grid_mid_y),
                            )
                            if (
                                w_candidate is not None
                                and w_candidate["left"] >= w_left_r
                                and w_candidate["left"] + w_candidate["layout"]["width"] <= w_right_l
                            ):
                                figure_placements = w_places
                                candidate = w_candidate
                legend_placement = candidate
            else:
                legend_placement = _choose_free_space_legend(
                    shared_legend_items,
                    occupied_boxes,
                    deck.slide_width,
                    margin,
                    content_top,
                    full_bottom,
                )

            if legend_placement is None:
                # No existing free slot was large enough; reserve a compact bottom legend strip.
                strip_layout = _layout_legend_box(
                    shared_legend_items,
                    (margin, 0, deck.slide_width - margin, Inches(1.4)),
                    prefer_vertical=False,
                    font_candidates=(9.5, 9.0, 8.5),
                )
                if strip_layout is not None:
                    bottom_pad = Inches(0.14)
                    strip_gap = Inches(0.08)
                    strip_bottom = deck.slide_height - bottom_pad
                    strip_top = strip_bottom - strip_layout["height"]
                    reserved_grid_bottom = strip_top - strip_gap
                    figure_placements, _ = _compute_grid_placements(reserved_grid_bottom)
                    bx, by = _place_legend_box_in_rect(
                        strip_layout,
                        (margin, strip_top, deck.slide_width - margin, strip_bottom),
                        deck.slide_width,
                        content_top,
                        full_bottom,
                    )
                    legend_placement = {"left": bx, "top": by, "layout": strip_layout}

        for placed in figure_placements:
            item = placed["item"]
            relative_path = item["relative_path"]
            left = placed["left"]
            top = placed["top"]
            width = placed["width"]
            height = placed["height"]
            label_height = placed["label_height"]
            picture = slide.shapes.add_picture(BytesIO(item["data"]), left, top, width, height)
            picture.name = relative_path
            caption = slide.shapes.add_textbox(
                left - caption_width - caption_gap,
                top + (height - label_height) // 2,
                caption_width,
                label_height,
            )
            caption.name = f"Caption {relative_path}"
            frame = caption.text_frame
            frame.margin_left = frame.margin_right = frame.margin_top = frame.margin_bottom = 0
            frame.word_wrap = False
            frame.auto_size = MSO_AUTO_SIZE.TEXT_TO_FIT_SHAPE
            frame.vertical_anchor = MSO_ANCHOR.MIDDLE
            paragraph = frame.paragraphs[0]
            paragraph.text = placed["caption_text"]
            paragraph.alignment = PP_ALIGN.RIGHT
            paragraph.font.size = Pt(12)
            paragraph.font.bold = True

        if legend_placement is not None and shared_legend_items:
            _draw_shared_legend_box(slide, shared_legend_items, legend_placement)

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
    copy_used_recipes(plan, output)
    return destination
