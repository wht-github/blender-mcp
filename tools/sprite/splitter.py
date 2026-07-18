"""Sprite-sheet grid splitter.

Splits a uniformly gridded sprite sheet (rows x cols) into individual PNG frames.

Algorithm:
    - Detect grid via simple row/column uniformity test: detect rows/cols that
      are entirely equal to the "background" colour, OR just trust the caller
      rows/cols parameters and divide evenly.
    - Default mode is even division (rows x cols), which is fastest and robust.
    - Optionally auto-detect grid counts by edge counting if caller omits them.

This module has no third-party dependencies; it operates on raw PNG / Pillow.
To keep the host `blender_agent` plugin clean, Pillow is declared in
`tools/sprite/requirements.txt`, NOT in `pyproject.toml`.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Iterable

if TYPE_CHECKING:  # pragma: no cover - typing only
    from PIL.Image import Image as PILImage


# ---------------------------------------------------------------------------
# Public data types
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class FrameInfo:
    """Description of a single extracted frame."""

    index: int
    row: int
    col: int
    x: int
    y: int
    width: int
    height: int
    out_path: Path


# ---------------------------------------------------------------------------
# Core splitting
# ---------------------------------------------------------------------------

def iter_frame_grid(
    img: "PILImage",
    rows: int,
    cols: int,
    *,
    margin: int = 0,
) -> Iterable[tuple[int, int, int, int, int]]:
    """Yield (row, col, x, y, frame_w, frame_h) for every frame cell.

    The image is divided into rows*cols equal cells. `margin` trims each cell
    inward (in pixels) before emitting the crop region.
    """
    if rows < 1 or cols < 1:
        raise ValueError(f"rows and cols must be >=1, got rows={rows} cols={cols}")
    if margin < 0:
        raise ValueError(f"margin must be >=0, got {margin}")

    cell_w = img.width // cols
    cell_h = img.height // rows
    if cell_w < 1 or cell_h < 1:
        raise ValueError(
            f"Grid {rows}x{cols} over image {img.width}x{img.height} yields empty cells"
        )

    # Remainder pixels (if image isn't an exact multiple) are discarded from the
    # bottom-right edge — this matches typical sprite-sheet authoring intent.
    usable_w = cell_w * cols
    usable_h = cell_h * rows
    offset_x = (img.width - usable_w) // 2
    offset_y = (img.height - usable_h) // 2

    for row in range(rows):
        for col in range(cols):
            x = offset_x + col * cell_w + margin
            y = offset_y + row * cell_h + margin
            w = max(cell_w - 2 * margin, 1)
            h = max(cell_h - 2 * margin, 1)
            yield row, col, x, y, w, h


def split(
    image_path: Path,
    out_dir: Path,
    *,
    rows: int | None = None,
    cols: int | None = None,
    prefix: str | None = None,
    margin: int = 0,
    start_index: int = 1,
    overwrite: bool = True,
) -> list[FrameInfo]:
    """Split a sprite sheet into individual PNG files.

    Args:
        image_path: Source image (PNG/JPG/etc. — anything Pillow can open).
        out_dir: Destination directory (created if missing).
        rows: Number of rows in the sprite grid. None = auto-detect.
        cols: Number of columns. None = auto-detect.
        prefix: Filename prefix; defaults to the source image stem.
        margin: Pixels to trim from each cell's edges before cropping.
        start_index: Frame numbering base (default 1).
        overwrite: If False, raise FileExistsError rather than over-writing.

    Returns:
        List of :class:`FrameInfo` for every emitted frame, sorted by reading order.
    """
    from PIL import Image  # imported lazily so import of this module stays free

    image_path = Path(image_path)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    with Image.open(image_path) as src:
        src.load()
        img = src.convert("RGBA")  # ensure predictable channel count for crops

        if rows is None or cols is None:
            detected_rows, detected_cols = _detect_grid(img)
            rows = rows or detected_rows
            cols = cols or detected_cols

        name_prefix = prefix if prefix is not None else image_path.stem
        results: list[FrameInfo] = []
        idx = start_index
        for row, col, x, y, w, h in iter_frame_grid(
            img, rows, cols, margin=margin
        ):
            crop = img.crop((x, y, x + w, y + h))
            out_path = out_dir / f"{name_prefix}_{idx:03d}.png"
            if out_path.exists() and not overwrite:
                raise FileExistsError(out_path)
            crop.save(out_path, format="PNG")
            results.append(
                FrameInfo(
                    index=idx,
                    row=row,
                    col=col,
                    x=x,
                    y=y,
                    width=w,
                    height=h,
                    out_path=out_path,
                )
            )
            idx += 1
    return results


# ---------------------------------------------------------------------------
# Auto detection (best-effort)
# ---------------------------------------------------------------------------

def _detect_grid(img: "PILImage") -> tuple[int, int]:
    """Best-effort detect grid size by scanning for transparent gutter rows/cols.

    Works when the sprite sheet has a transparent background and visible content
    rectangles are separated by fully transparent rows/cols. Falls back to (1, 1)
    if no gutter can be found.
    """
    # Importing here keeps the top-level module importable without Pillow.
    alpha = img.getchannel("A") if img.mode == "RGBA" else None
    if alpha is None:
        raise ValueError(
            "Auto-detect requires an RGBA image with a transparent background; "
            "pass rows/cols explicitly."
        )

    rows = _count_transparent_gutter_lines(alpha, axis="horizontal")
    cols = _count_transparent_gutter_lines(alpha, axis="vertical")
    return max(rows, 1), max(cols, 1)


def _count_transparent_gutter_lines(alpha: "PILImage", *, axis: str) -> int:
    """Return number of content bands along the given axis.

    A transparent gutter is a full row (axis=horizontal) or column (axis=vertical)
    whose alpha values are all 0. Content bands separated by transparent gutters
    are counted; each band is one sprite row or column.

    Uses pure-Python scans via :meth:`PIL.Image.Image.getdata` to avoid pulling in
    numpy as an extra runtime dependency.
    """
    if axis not in ("horizontal", "vertical"):
        raise ValueError(f"unknown axis: {axis}")

    # Scala­r transpose: iterate per-line lazily.
    if axis == "horizontal":
        # Count rows whose every pixel alpha is 0.
        line_is_empty = [
            all(px == 0 for px in alpha.crop((0, y, alpha.width, y + 1)).getdata())
            for y in range(alpha.height)
        ]
    else:
        line_is_empty = [
            all(px == 0 for px in alpha.crop((x, 0, x + 1, alpha.height)).getdata())
            for x in range(alpha.width)
        ]

    bands = 0
    in_band = False
    for is_empty in line_is_empty:
        if not is_empty and not in_band:
            bands += 1
            in_band = True
        elif is_empty:
            in_band = False
    return bands or 1
