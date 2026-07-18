# Repo tools

Standalone helper tools that ship alongside `blender_agent` but are **not** part
of the Blender addon bundle. They are meant to assist local AI toolchains
(sprite sheets, frame previews, asset prep, etc.).

These tools MUST NOT introduce runtime dependencies into
`blender_agent/libs/`. Dependency isolation rules:

* Default `[project.dependencies]` and `runtime-requirements.txt` feed the
  blended addon wheel. **Never list tool-only deps there.**
* Each tool keeps its own `requirements.txt` next to its source files.
* Run tools via `uv run --with-requirements <tool>/requirements.txt ...` to get
  a throwaway environment that does not touch the host workspace.

## `sprite/` — Sprite sheet splitter

Splits a uniformly gridded sprite sheet (rows × cols) into individual PNG
frames. Useful for preparing frame-by-frame animation assets before feeding them
to the Blender agent or other AI tools.

### Quick usage

```powershell
# 1. Explicit grid (recommended; works on opaque sprite sheets)
uv run --with-requirements tools\sprite\requirements.txt `
       python tools\sprite\split.py examples\imgs\陀螺精灵图.png `
       --rows 2 --cols 3

# 2. Auto-detect grid (requires transparent background)
uv run --with-requirements tools\sprite\requirements.txt `
       python tools\sprite\split.py path\to\transparent_sheet.png --auto

# 3. Importable from another Python process
import sys; sys.path.insert(0, r"D:\Workspace\agent\blender-agent\tools\sprite")
from splitter import split
frames = split(Path("sprite.png"), Path("out"), rows=2, cols=3)
```

Outputs land in `<image-stem>_frames/` by default, named `<stem>_001.png`,
`<stem>_002.png`, ... in reading order (row-major). Use `--out`, `--prefix`,
`--start`, and `--margin` to customise.

### AI skill

See [`../docs/skills/sprite_sheet_split.md`](../docs/skills/sprite_sheet_split.md)
for a prompt-side skill that other workspaces can `@import` to learn how to
invoke this tool.
