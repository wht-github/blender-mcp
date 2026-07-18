#!/usr/bin/env python3
"""Sprite sheet splitter CLI.

Usage examples:

    # Basic 2x3 split into ./out
    uv run --with pillow python tools/sprite/split.py sprite.png --rows 2 --cols 3

    # Auto-detect grid (requires transparent background)
    uv run --with pillow python tools/sprite/split.py sprite.png --auto

    # Trim 4px edges off each frame
    uv run --with pillow python tools/sprite/split.py sprite.png -r 2 -c 3 --margin 4

Run as a standalone script; Pillow is pulled in via ``--with pillow`` so that no
dependency leaks into the blender_agent addon bundle.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Make sibling `splitter` importable when executed via absolute path.
if __name__ == "__main__" and __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent))

from splitter import FrameInfo, split  # type: ignore[import-not-found]  # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="sprite-split",
        description="Split a uniformly gridded sprite sheet into individual PNG frames.",
    )
    p.add_argument("image", type=Path, help="Path to the source sprite sheet.")
    p.add_argument(
        "-o", "--out", type=Path, default=None,
        help="Output directory (default: <image-stem>_frames/ next to the image).",
    )
    p.add_argument("-r", "--rows", type=int, default=None, help="Grid rows.")
    p.add_argument("-c", "--cols", type=int, default=None, help="Grid columns.")
    p.add_argument(
        "--auto", action="store_true",
        help="Auto-detect rows/cols via transparent gutters (requires RGBA image).",
    )
    p.add_argument(
        "--margin", type=int, default=0,
        help="Pixels to trim from each cell edge before saving.",
    )
    p.add_argument(
        "--prefix", default=None,
        help="Filename prefix (default: source image stem).",
    )
    p.add_argument(
        "--start", type=int, default=1,
        help="Frame numbering base (default: 1).",
    )
    p.add_argument(
        "--no-overwrite", action="store_true",
        help="Fail instead of over-writing existing output files.",
    )
    p.add_argument(
        "--quiet", action="store_true",
        help="Only print errors; exit 0 on success.",
    )
    return p


def _resolve_out_dir(image: Path, out: Path | None) -> Path:
    if out is not None:
        return out
    return image.parent / f"{image.stem}_frames"


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.auto and (args.rows is not None or args.cols is not None):
        print("--auto conflicts with explicit --rows/--cols", file=sys.stderr)
        return 2

    if not args.image.exists():
        print(f"image not found: {args.image}", file=sys.stderr)
        return 2

    out_dir = _resolve_out_dir(args.image, args.out)

    try:
        results = split(
            args.image,
            out_dir,
            rows=args.rows if not args.auto else None,
            cols=args.cols if not args.auto else None,
            prefix=args.prefix,
            margin=args.margin,
            start_index=args.start,
            overwrite=not args.no_overwrite,
        )
    except Exception as exc:  # noqa: BLE001 - CLI boundary, surface message
        print(f"split failed: {exc}", file=sys.stderr)
        return 1

    if args.quiet:
        return 0

    print(f"split {args.image.name} -> {out_dir} ({len(results)} frames)")
    for info in results:
        print(
            f"  #{info.index:03d} r{info.row}c{info.col} "
            f"{info.width}x{info.height} {info.out_path.name}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
