from PIL import Image, ImageDraw, ImageFont
from typing import Optional, Tuple

def _load_fonts(size: int):
    """Load the handful of font weights we need for stat cards."""
    return {
        "regular": ImageFont.truetype("./static/Inter-Regular.ttf", int(size * 0.055)),
        "name_bold": ImageFont.truetype("./static/Inter-Bold.ttf", int(size * 0.055)),
        "small": ImageFont.truetype("./static/Inter-Regular.ttf", int(size * 0.042)),
        "small_light": ImageFont.truetype("./static/Inter-Light.ttf", int(size * 0.042)),
        "big": ImageFont.truetype("./static/Inter-Black.ttf", int(size * 0.23)),
        "stat_big": ImageFont.truetype("./static/Inter-Black.ttf", int(size * 0.28)),
        "hours": ImageFont.truetype("./static/Inter-Black.ttf", int(size * 0.18)),
        "cta": ImageFont.truetype("./static/Inter-Light.ttf", int(size * 0.045)),
    }


def _wrap_text_with_ellipsis(text: str, font: ImageFont.FreeTypeFont, draw: ImageDraw.ImageDraw, max_width: int, max_lines: int = 2):
    """Wrap text to a maximum number of lines; if overflow, ellipsize the final line."""
    if not text:
        return []
    words = text.split()
    if not words:
        return [text[:max_width]]

    def width(t: str) -> int:
        bbox = draw.textbbox((0, 0), t, font=font)
        return (bbox[2] - bbox[0]) if bbox else 0

    lines = []
    idx = 0

    # Build all but last line
    while idx < len(words) and len(lines) < max_lines - 1:
        line_words = []
        while idx < len(words):
            candidate = " ".join(line_words + [words[idx]])
            if width(candidate) <= max_width:
                line_words.append(words[idx])
                idx += 1
            else:
                break

        if not line_words:
            # Single word too long; hard truncate it
            word = words[idx]
            while word and width(word + "...") > max_width:
                word = word[:-1]
            if word:
                line_words.append(word + "...")
                idx += 1
            else:
                break

        lines.append(" ".join(line_words))

    # Last line with ellipsis as needed
    if idx < len(words):
        line_words = []
        while idx < len(words):
            candidate = " ".join(line_words + [words[idx]])
            candidate_ellipsis = candidate + ("..." if idx < len(words) - 1 else "")
            if width(candidate_ellipsis) <= max_width:
                line_words.append(words[idx])
                idx += 1
            else:
                break

        last_line = " ".join(line_words) if line_words else ""

        if idx < len(words):
            # Need ellipsis; trim until it fits
            while last_line and width(last_line + "...") > max_width:
                last_line = last_line[:-1].rstrip()
            if last_line:
                last_line = last_line.rstrip() + "..."
            else:
                last_line = "..."

        lines.append(last_line)
    elif idx == len(words) and len(lines) < max_lines:
        # We built fewer than max_lines and consumed all words
        pass

    return lines[:max_lines]


def _load_static_tile(path: str, size: int, label: str, background=(24, 24, 32)):
    """Load a static PNG; if missing, return a simple placeholder."""
    try:
        img = Image.open(path).convert("RGB")
        if img.size != (size, size):
            resample = Image.Resampling.LANCZOS if hasattr(Image, "Resampling") else Image.LANCZOS
            img = img.resize((size, size), resample)
        return img
    except Exception as exc:
        img = Image.new("RGB", (size, size), background)
        draw = ImageDraw.Draw(img)
        fonts = _load_fonts(size)
        warning = f"{label}\nmissing"
        draw.multiline_text((int(size * 0.08), int(size * 0.45)), warning, font=fonts["small"], fill=(200, 200, 200), spacing=int(size * 0.02))
        print(f"Warning: failed to load static tile {path}: {exc}")
        return img

def render_general_stat_card(
    output_path: Optional[str],
    value,
    header_text: str,
    detail_text: str = "",
    small_text: str = "",
    background: Tuple[int, int, int] = (15, 23, 42),     # slate
    foreground: Tuple[int, int, int] = (226, 232, 240),  # light slate
    accent: Tuple[int, int, int] = (34, 211, 238),       # cyan
    size: int = 1080,
    value_format: Optional[str] = None,
    cta_text: Optional[str] = None,
    offsets: Optional[list] = None,
    return_image: bool = False,
):
    """
    Generic stat card used by most slides (everything except busiest month, top classmates,
    summary grid, and the procrastination special). Bigger stat, no unit label.
    Accepts optional small_text for a second line of detail.
    """
    if not offsets:
        offsets = [None, None, None, None]
    img = Image.new("RGB", (size, size), background)
    draw = ImageDraw.Draw(img)
    fonts = _load_fonts(size)

    left = int(size * 0.08)
    top = int(size * 0.10)
    y = top

    draw.multiline_text(
        (left, y),
        header_text,
        font=fonts["regular"],
        fill=foreground,
        spacing=int(size * 0.02),
    )
    y += int(size * (0.15 if not offsets[0] else offsets[0]))

    if value_format:
        stat_text = value_format.format(value=value)
    elif isinstance(value, (int, float)):
        stat_text = f"{value:,.0f}" if float(value).is_integer() else f"{value:,.1f}"
    else:
        stat_text = str(value)

    draw.text(
        (left, y),
        stat_text,
        font=fonts["stat_big"],
        fill=accent,
    )
    y += int(size * (0.30 if not offsets[1] else offsets[1]))

    if detail_text:
        draw.text(
            (left, y),
            detail_text,
            font=fonts["regular"],
            fill=foreground,
        )
        y += int(size * (0.10 if not offsets[2] else offsets[2]))

    if small_text:
        draw.text(
            (left, y),
            small_text,
            font=fonts["small"],
            fill=foreground,
        )
        y += int(size * (0.08 if not offsets[3] else offsets[3]))

    if cta_text:
        cta_bbox = draw.textbbox((0, 0), cta_text, font=fonts["cta"])
        cta_height = cta_bbox[3] - cta_bbox[1]
        draw.text(
            (left, size - cta_height - int(size * 0.08)),
            cta_text,
            font=fonts["cta"],
            fill=accent,
        )

    if output_path:
        img.save(output_path, format="PNG")
        print(f"Saved stat card to {output_path}")

    return img if return_image or not output_path else None

def render_procrast_stat_card(
    output_path: Optional[str],
    hours: float,
    background: Tuple[int, int, int] = (239, 68, 68),     # red
    foreground: Tuple[int, int, int] = (255, 255, 255),  # white
    accent: Tuple[int, int, int] = (253, 224, 71),       # yellow
    size: int = 1080,
    cta_text: Optional[str] = None,
    return_image: bool = False,
):
    """
    Generate a shareable stat card image.

    Parameters:
        output_path: where to save the PNG
        hours: numeric stat (e.g. 51.3)
        background: RGB tuple for background
        foreground: RGB tuple for normal text
        accent: RGB tuple for highlighted text
        size: square image size in px (default 1080)
    """

    # --- Canvas ---
    img = Image.new("RGB", (size, size), background)
    draw = ImageDraw.Draw(img)

    fonts = _load_fonts(size)

    # --- Layout constants ---
    left = int(size * 0.08)
    top = int(size * 0.10)
    line_gap = int(size * 0.02)

    y = top

    # --- Header ---
    header_text = "I submitted my\nassignments"
    draw.multiline_text(
        (left, y),
        header_text,
        font=fonts["regular"],
        fill=foreground,
        spacing=line_gap,
    )

    y += int(size * 0.14)

    # --- Big number ---
    hours_text = f"{hours:.1f}"
    draw.text(
        (left, y),
        hours_text,
        font=fonts["big"],
        fill=accent,
    )

    y += int(size * 0.24)

    # --- "hours" ---
    draw.text(
        (left, y),
        "hours",
        font=fonts["hours"],
        fill=accent,
    )

    y += int(size * 0.22)

    # --- Qualifier ---
    draw.text(
        (left, y),
        "before the deadline",
        font=fonts["regular"],
        fill=foreground,
    )

    y += int(size * 0.07)

    draw.text(
        (left, y),
        "on average",
        font=fonts["small"],
        fill=foreground,
    )

    # --- CTA (bottom anchored) ---
    if cta_text:
        cta_bbox = draw.textbbox((0, 0), cta_text, font=fonts["cta"])
        cta_height = cta_bbox[3] - cta_bbox[1]

        draw.text(
            (left, size - cta_height - int(size * 0.08)),
            cta_text,
            font=fonts["cta"],
            fill=accent,
        )

    # --- Save ---
    if output_path:
        img.save(output_path, format="PNG")
        print(f"Saved stat card to {output_path}")

    return img if return_image or not output_path else None


def render_top_classmates_card(
    output_path: Optional[str],
    classmates,
    background: Tuple[int, int, int] = (26, 26, 46),      # deep indigo
    foreground: Tuple[int, int, int] = (234, 234, 234),  # light gray
    accent: Tuple[int, int, int] = (14, 165, 233),       # cyan
    size: int = 1080,
    cta_text: Optional[str] = None,
    return_image: bool = False,
):
    """
    Render top classmates (max 3). Names use bold at the regular size; details use light small.
    Expected classmate shape: {"name": str, "detail": str} or {"name": str, "sections": [...] }.
    """
    img = Image.new("RGB", (size, size), background)
    draw = ImageDraw.Draw(img)
    fonts = _load_fonts(size)

    left = int(size * 0.08)
    top = int(size * 0.10)
    max_width = int(size * 0.84)
    y = top

    draw.text(
        (left, y),
        "My top classmates",
        font=fonts["name_bold"],
        fill=foreground,
    )
    y += int(size * 0.12)

    # Slightly smaller fonts for shareable readability
    name_font = ImageFont.truetype("./static/Inter-Bold.ttf", int(size * 0.05))
    detail_font = ImageFont.truetype("./static/Inter-Light.ttf", int(size * 0.038))

    name_line_height = draw.textbbox((0, 0), "Ag", font=name_font)[3]
    detail_line_height = draw.textbbox((0, 0), "Ag", font=detail_font)[3]

    row_gap = int(size * 0.04)

    for idx, cls in enumerate(classmates[:3]):
        name = cls.get("name", f"Classmate {idx + 1}")
        detail = cls.get("detail")
        if not detail:
            sections = cls.get("sections")
            if isinstance(sections, (list, tuple)):
                detail = ", ".join(str(s) for s in sections[:4])
            elif sections:
                detail = str(sections)
        detail = detail or "Shared classes"

        name_lines = _wrap_text_with_ellipsis(name, name_font, draw, max_width)
        for line in name_lines:
            draw.text(
                (left, y),
                line,
                font=name_font,
                fill=accent,
            )
            y += name_line_height + int(size * 0.008)

        detail_lines = _wrap_text_with_ellipsis(detail, detail_font, draw, max_width)
        for line in detail_lines:
            draw.text(
                (left, y),
                line,
                font=detail_font,
                fill=foreground,
            )
            y += detail_line_height + int(size * 0.004)

        y += row_gap

    if cta_text:
        cta_bbox = draw.textbbox((0, 0), cta_text, font=fonts["cta"])
        cta_height = cta_bbox[3] - cta_bbox[1]
        draw.text(
            (left, size - cta_height - int(size * 0.08)),
            cta_text,
            font=fonts["cta"],
            fill=accent,
        )

    if output_path:
        img.save(output_path, format="PNG")
        print(f"Saved stat card to {output_path}")

    return img if return_image or not output_path else None


def render_busiest_month_card(
    output_path: Optional[str],
    month_name: str,
    detail_text: str = "",
    background: Tuple[int, int, int] = (250, 214, 115),   # warm yellow
    foreground: Tuple[int, int, int] = (124, 45, 18),     # dark brown
    accent: Tuple[int, int, int] = (234, 88, 12),         # orange
    size: int = 1080,
    cta_text: Optional[str] = None,
    return_image: bool = False,
):
    """
    Dedicated card for Busiest Month. Month name uses the same scale as the
    procrastination “hours” label (large, but smaller than main stats).
    """
    img = Image.new("RGB", (size, size), background)
    draw = ImageDraw.Draw(img)
    fonts = _load_fonts(size)

    left = int(size * 0.08)
    top = int(size * 0.10)
    y = top

    draw.text(
        (left, y),
        "My busiest month was",
        font=fonts["regular"],
        fill=foreground,
    )
    y += int(size * 0.08)

    draw.text(
        (left, y),
        month_name,
        font=fonts["hours"],  # similar scale to procrastination "hours"
        fill=accent,
    )
    y += int(size * 0.24)

    if detail_text:
        draw.text(
            (left, y),
            detail_text,
            font=fonts["regular"],
            fill=foreground,
        )
        y += int(size * 0.15)

    if cta_text:
        cta_bbox = draw.textbbox((0, 0), cta_text, font=fonts["cta"])
        cta_height = cta_bbox[3] - cta_bbox[1]
        draw.text(
            (left, size - cta_height - int(size * 0.08)),
            cta_text,
            font=fonts["cta"],
            fill=accent,
        )

    if output_path:
        img.save(output_path, format="PNG")
        print(f"Saved stat card to {output_path}")

    return img if return_image or not output_path else None


def render_recap_grid(
    output_path: str,
    data: dict,
    tile_size: int = 600,
    gap: Optional[int] = None,
    static_title_path: str = "./static/Slide_center-title.png",
    static_cta_path: str = "./static/Slide_CTA.png",
):
    """
    Build 3×3 grid:
      Row 1: assignments, late night, busiest month
      Row 2: weekday submissions, static title, top classmates
      Row 3: weekend submissions, avg hours before deadline, static CTA
    """
    g = gap if gap is not None else int(tile_size * 0.05)
    width = tile_size * 3 + g * 4  # includes outer border
    height = tile_size * 3 + g * 4
    canvas = Image.new("RGB", (width, height), (0, 0, 0))

    total_assignments = data.get("total_assignments", 0)
    course_count = data.get("course_count", 0)
    late_night = data.get("late_night_submissions", data.get("night_owl_subs", 0))
    busiest_month = data.get("busiest_month", "October")
    busiest_month_assignments = data.get("busiest_month_assignments", data.get("assignments_bm", 0))
    weekend_subs = data.get("weekend_submissions", data.get("weekend_subs", 0))
    avg_hours = data.get("avg_hours_before_deadline", data.get("avg_procrastination", 0.0))
    top_classmates = data.get("top_classmates", [])

    # Tile 1
    tile1 = render_general_stat_card(
        None,
        total_assignments,
        "I had",
        "assignments in Schoology",
        small_text=f"across {course_count} courses",
        background=(15, 23, 42),
        foreground=(226, 232, 240),
        accent=(34, 211, 238),
        size=tile_size,
        offsets=[0.12, 0.30, 0.10, 0.08],
        return_image=True,
    )

    # Tile 2
    tile2 = render_general_stat_card(
        None,
        late_night,
        "I submitted",
        "assignments to Schoology",
        small_text="past 10pm",
        background=(12, 23, 40),
        foreground=(226, 232, 240),
        accent=(34, 211, 238),
        size=tile_size,
        offsets=[0.12, 0.30, 0.10, 0.08],
        return_image=True,
    )

    # Tile 3
    tile3 = render_busiest_month_card(
        None,
        busiest_month,
        detail_text=f"With {busiest_month_assignments} assignments",
        size=tile_size,
        return_image=True,
    )

    # Tile 4 (weekday submissions)
    tile4 = render_general_stat_card(
        None,
        data.get("weekday_submissions", data.get("weekday_subs", 0)),
        "I submitted",
        "assignments to Schoology",
        small_text="on weekdays",
        background=(12, 23, 40),
        foreground=(226, 232, 240),
        accent=(34, 211, 238),
        size=tile_size,
        offsets=[0.10, 0.32, 0.09, 0.08],
        return_image=True,
    )

    # Tile 5 (static title)
    tile5 = _load_static_tile(static_title_path, tile_size, "Title")

    # Tile 6 (top classmates)
    tile6 = render_top_classmates_card(
        None,
        top_classmates,
        background=(20, 21, 35),
        foreground=(230, 234, 240),
        accent=(14, 165, 233),
        size=tile_size,
        return_image=True,
    )

    # Tile 7
    tile7 = render_general_stat_card(
        None,
        weekend_subs,
        "I submitted",
        "assignments to Schoology",
        small_text="on weekends",
        background=(10, 22, 37),
        foreground=(226, 232, 240),
        accent=(34, 211, 238),
        size=tile_size,
        offsets=[0.12, 0.30, 0.10, 0.08],
        return_image=True,
    )

    # Tile 8 (time before deadline)
    tile8 = render_procrast_stat_card(
        None,
        avg_hours,
        background=(237, 110, 102),
        foreground=(255, 255, 255),
        accent=(253, 224, 71),
        size=tile_size,
        return_image=True,
    )

    # Tile 9 (static CTA)
    tile9 = _load_static_tile(static_cta_path, tile_size, "CTA")

    tiles = [tile1, tile2, tile3, tile4, tile5, tile6, tile7, tile8, tile9]

    for idx, tile in enumerate(tiles):
        row = idx // 3
        col = idx % 3
        x = g + col * (tile_size + g)
        y = g + row * (tile_size + g)
        canvas.paste(tile, (x, y))

    canvas.save(output_path, format="PNG")
    print(f"Saved recap grid to {output_path}")

if __name__ == "__main__":
    # Test procrastination card
    render_procrast_stat_card("test.png", 51.3)
    # Test total assignments card
    render_general_stat_card("test2.png", 1234, "I had", detail_text="assignments in Schoology\n\nacross 10 courses", offsets=[0.08, 0.35, 0.08])
    # Test weekday subs card
    render_general_stat_card("test3.png", 567, "I submitted", detail_text="assignments to Schoology\n\non weekdays", offsets=[0.08, 0.35, 0.08])
    # Test weekend subs card
    render_general_stat_card("test4.png", 123, "I submitted", detail_text="assignments to Schoology\n\non weekends", offsets=[0.08, 0.35, 0.08])
    # Test night owl subs card
    render_general_stat_card("test5.png", 456, "I submitted", detail_text="assignments to Schoology\n\npast 10pm", offsets=[0.08, 0.35, 0.08])
    # Test busiest month card
    render_busiest_month_card("test6.png", "October", detail_text="With 23 assignments")
    # Test top classmates card
    render_top_classmates_card(
        "test7.png",
        [
            {"name": "Alex Kim: 5 shared classes", "detail": "AP Statistics: B Period, AP Calculus: A Period, AP Computer Science: A Period, AP Physics: B Period, AP Chemistry: A Period"},
            {"name": "Jordan Lee: 4 shared classes", "detail": "AP Statistics: B Period, AP Calculus: A Period, AP Computer Science: A Period, AP Physics: B Period"},
            {"name": "Taylor Smith: 3 shared classes", "detail": "AP Statistics: B Period, AP Calculus: A Period, AP Computer Science: A Period"},
        ],
    )
    # Test full grid build
    sample_data = {
        "total_assignments": 789,
        "course_count": 7,
        "late_night_submissions": 123,
        "busiest_month": "October",
        "busiest_month_assignments": 23,
        "weekend_submissions": 87,
        "avg_hours_before_deadline": 5.4,
        "top_classmates": [
            {"name": "Alex Kim", "count": 5, "sections": ["AP Statistics: B Period", "AP Calculus: A Period", "AP Computer Science: A Period", "AP Physics: B Period", "AP Chemistry: A Period"]},
            {"name": "Jordan Lee", "count": 4, "sections": ["AP Statistics: B Period", "AP Calculus: A Period", "AP Computer Science: A Period", "AP Physics: B Period"]},
            {"name": "Taylor Smith", "count": 3, "sections": ["AP Statistics: B Period", "AP Calculus: A Period", "AP Computer Science: A Period"]},
        ],
    }
    render_recap_grid("test_grid.png", sample_data)
