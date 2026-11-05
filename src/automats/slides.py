"""Build a local PowerPoint deck from the recipe's resolved PNG rows."""

from io import BytesIO
import os
from pathlib import Path
import tempfile

from automats.config import Plan


class SlideError(ValueError):
    pass


def build_slides(plan: Plan, output: Path) -> Path:
    if plan.slide_error is not None:
        raise SlideError(plan.slide_error)
    if not plan.slides:
        raise SlideError("No slides configured; add a slides section to the recipe")
    try:
        from PIL import Image
        from pptx import Presentation
        from pptx.enum.text import MSO_ANCHOR, MSO_AUTO_SIZE, PP_ALIGN
        from pptx.util import Inches, Pt
    except ImportError as error:
        raise SlideError("PowerPoint dependencies are missing; rerun AutoMATS setup") from error

    deck = Presentation()
    deck.slide_width = Inches(13 + 1 / 3)
    deck.slide_height = Inches(7.5)
    margin = Inches(0.25)
    gap = Inches(0.12)
    caption_width = Inches(0.65)
    caption_gap = Inches(0.08)
    caption_height = Inches(0.32)
    content_top = Inches(0.9)
    for index, spec in enumerate(plan.slides, 1):
        slide = deck.slides.add_slide(deck.slide_layouts[6])
        heading = slide.shapes.add_textbox(margin, Inches(0.2),
                                          deck.slide_width - 2 * margin, Inches(0.5))
        heading.text_frame.word_wrap = True
        heading.text_frame.auto_size = MSO_AUTO_SIZE.TEXT_TO_FIT_SHAPE
        paragraph = heading.text_frame.paragraphs[0]
        paragraph.text = spec.title
        paragraph.alignment = PP_ALIGN.CENTER
        paragraph.font.size = Pt(24)
        paragraph.font.bold = True
        row_height = (deck.slide_height - content_top - margin - gap * (len(spec.rows) - 1)) // len(spec.rows)
        for row_number, row in enumerate(spec.rows, 1):
            cell_width = (deck.slide_width - 2 * margin - gap * (len(row) - 1)) // len(row)
            image_width = cell_width - 2 * (caption_width + caption_gap)
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
                except (OSError, ValueError) as error:
                    raise SlideError(f"Slide {index} ({spec.plot_group}), row {row_number}: "
                                     f"cannot use {path}: {error}") from error
                scale = min(image_width / pixel_width, row_height / pixel_height)
                width = max(1, int(pixel_width * scale))
                height = max(1, int(pixel_height * scale))
                left = margin + column * (cell_width + gap) + (cell_width - width) // 2
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