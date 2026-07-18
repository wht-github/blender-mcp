<!--
  Skill: sprite_sheet_split
  Purpose: Teach an AI agent how to split a uniformly gridded sprite sheet
           into individual PNG frames using the standalone tool shipped in
           this repo's `tools/sprite/` directory.

  This file is safe to `@import` from any workspace that can reach this repo
  by absolute path. No package installation is required on the consumer side.
-->

# Skill: Split a sprite sheet into individual frames

> **NEVER assume the grid.** Sprite sheets vary — always discover `rows` and
> `cols` per image (read dimensions → visual inspection → ask the user when
> not confident). Do not carry over numbers from a previous run.

## When to use

Invoke this skill when the user asks to:

- "拆分精灵图" / "split sprite sheet" / "extract animation frames"
- Turn a single grid sprite image into per-frame PNGs
- Prepare frame assets for animation pipelines (Blender, game engines, AI refs)

Do **not** use for: non-grid sprite atlases with packed rectangles, JSON-packed
atlases (use the atlas's metadata instead), or GIF → PNG sequences (use
`ffmpeg` or `Pillow.ImageSequence`).

## Prerequisites

The tool lives at `<repo>/tools/sprite/split.py`. Its only third-party
dependency is `pillow`. Run with `uv run --with-requirements` to get an
isolated, throwaway env — this keeps the consumer workspace clean.

Replace `$REPO` below with the absolute path to this repository on the host
(=e.g. `D:\Workspace\agent\blender-agent`). Do **not** hard-code any image
dimensions, rows, or columns in skill state — always discover them per image.

## Procedure

> **Golden rule:** never assume the grid. Discover it per image, and when in
> doubt ask the user with `vscode_askQuestions` before running the splitter.

### 1. Read the image size

Every output frame is `width/cols × height/rows`, so you need the source
dimensions first. Read PNG dimensions directly (no Pillow needed):

```powershell
uv run python -c "import struct; d=open(r'<image>','rb').read(); `
  w,h=struct.unpack('>II', d[16:24]); print(f'{w}x{h}')"
```

Record (width, height) for the next step.

### 2. Discover rows × cols

Try these sources **in order**; stop at the first that yields a confident
answer:

1. **User-provided metadata.** If the request text or a sibling JSON/atlas
   file already states rows/cols, reuse that.
2. **Visual inspection (preferred for opaque sheets).** Use a multimodal image
   tool (e.g. `mcp_zai-mcp-serve_analyze_image`, or `view_image` / chat
   attachments) to actually *look* at the sheet and count rows and columns.
   This works for any background, transparent or not.
3. **Transparent-gutter auto-detect** — only if the image has an alpha channel
   and visibly transparent gutters between frames. In that case pass `--auto`
   and skip steps 3–4.
4. **Ask the user.** If steps 1–3 did not produce a confident, unambiguous
   `rows × cols`, do **not** guess. Use `vscode_askQuestions` (see §3 below).

### 3. Ask the user (when not confident)

Use the `vscode_askQuestions` tool with concise options. Suggest plausible
candidates derived from the image dimensions (e.g. for 1536×1024 the likely
splits are 2×3 → 512×512, 4×6 → 256×256, 1×6 ribbon, etc.). Always allow a
free-form fallback.

Example call:

```jsonc
vscode_askQuestions({
  "questions": [
    {
      "header": "rows",
      "question": "这张精灵图有几行？",
      "options": [
        { "label": "2", "description": "对应单帧高 512px" },
        { "label": "4", "description": "对应单帧高 256px" },
        { "label": "1", "description": "单行条带" }
      ],
      "allowFreeformInput": true
    },
    {
      "header": "cols",
      "question": "这张精灵图有几列？",
      "options": [
        { "label": "3", "description": "对应单帧宽 512px" },
        { "label": "6", "description": "对应单帧宽 256px" },
        { "label": "8", "description": "对应单帧宽 192px" }
      ],
      "allowFreeformInput": true
    }
  ]
})
```

Guidance for option lists:

- Always show the resulting single-frame `width×height` in each option's
  `description` so the user can sanity-check.
- Mark the most likely candidate `recommended: true` (usually the split whose
  cell size matches a power-of-two or a typical sprite size like 64/128/256/
  512).
- Allow at least 3 candidates plus free-form input. Never force a single
  option.
- If the user picks a combo that does not divide the source evenly, tell them
  the remainder is discarded (tool centers the grid on the usable area) and
  confirm before running.

### 4. Split into frames

Once `rows` (`R`) and `cols` (`C`) are confirmed:

```powershell
uv run --with-requirements "$REPO\tools\sprite\requirements.txt" `
       python "$REPO\tools\sprite\split.py" `
       "<image>" `
       --rows <R> --cols <C> `
       --out "<out_dir>" `
       [--prefix <name>] [--margin <px>] [--start <N>]
```

Add `--auto` instead of `--rows/--cols` **only** when step 3 of §2 applies
(transparent gutters). Do not mix `--auto` with explicit `--rows/--cols`.

### 5. Import from Python (alternative)

If the agent is already writing Python, prefer importing the library API.
Still discover `rows`/`cols` per §1–§3 above before calling:

```python
import sys
REPO = r"<absolute path to this repo>"
sys.path.insert(0, rf"{REPO}\tools\sprite")
from splitter import split  # noqa: E402
from pathlib import Path

frames = split(
    Path("sprite.png"),
    Path("out"),
    rows=R, cols=C,        # ← fill in after discovery / user confirmation
    # prefix="frame", margin=0, start_index=0,
)
for f in frames:
    print(f.index, f.row, f.col, f.out_path)
```

## Parameters reference

| Flag         | Default            | Notes                                       |
| ------------ | ------------------ | ------------------------------------------- |
| `--rows` `-r`| required (unless `--auto`) | Number of sprite rows.               |
| `--cols` `-c`| required (unless `--auto`) | Number of sprite columns.            |
| `--auto`     | off                | Detect rows/cols from transparent gutters.  |
| `--out` `-o` | `<stem>_frames/`   | Output directory (created if missing).      |
| `--prefix`   | source image stem  | Filename prefix.                            |
| `--start`    | `1`                | Frame numbering base (use `0` for zero-pad).|
| `--margin`   | `0`                | Pixels trimmed from each cell's edge.       |
| `--no-overwrite` | off            | Fail instead of over-writing.               |
| `--quiet`    | off                | Suppress per-frame listing.                 |

## Output convention

Files are written as `<prefix>_<NNN>.png` where `NNN` is zero-padded to three
digits starting from `--start`, in reading order (row 0 left→right, then row 1,
...). Each frame keeps the source's pixel data; opaque source → opaque frames.

## Safety / side effects

- Tool only reads the source image and writes into `--out`.
- No writes outside `--out`. Will not touch the blender_agent addon bundle.
- Pillow is installed in a `uv run` ephemeral env — it does **not** persist.

## Reference example

Split `examples\imgs\陀螺精灵图.png`:

1. **Read size** → 1536×1024.
2. **Discover grid.** Multimodal look or ask the user — confirm `2 rows × 3
   cols` (single-frame 512×512). Do **not** assume this; another image of the
   same dimensions might be a 4×6 or 1×6 layout.
3. **Confirm with the user** via `vscode_askQuestions` if there's any doubt
   (e.g. the visual is ambiguous, or the user didn't state rows/cols).
4. **Run** (after the user confirms R=2, C=3):

```powershell
uv run --with-requirements "$REPO\tools\sprite\requirements.txt" `
       python "$REPO\tools\sprite\split.py" `
       "$REPO\examples\imgs\陀螺精灵图.png" `
       --rows 2 --cols 3
```

Produces `examples\imgs\陀螺精灵图_frames\陀螺精灵图_001.png ... _006.png`,
each 512×512 RGBA.
