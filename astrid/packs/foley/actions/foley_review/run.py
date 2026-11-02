#!/usr/bin/env python3
"""Render a static review page pairing each tile clip with its Foley audio."""


from __future__ import annotations

from astrid.pack_api import guard_canonical_entrypoint

guard_canonical_entrypoint('foley.foley_review')
import argparse
import html
import json
import os
from datetime import datetime, timezone  # noqa: E402
from pathlib import Path
from typing import Any

from astrid.pack_api import build_manifest, write_manifest  # noqa: E402

PAGE_TEMPLATE = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>Foley Review — {video_name}</title>
  <style>
    :root {{
      --bg: #0e0e11;
      --fg: #e9e9ee;
      --muted: #9b9ba5;
      --card: #18181d;
      --border: #2a2a33;
      --good: #2ecc71;
      --bad: #ff5d6c;
    }}
    body {{ margin: 0; padding: 24px; background: var(--bg); color: var(--fg);
            font: 14px/1.45 system-ui, -apple-system, "Helvetica Neue", sans-serif; }}
    h1 {{ font-size: 18px; font-weight: 600; margin: 0 0 4px; }}
    .meta {{ color: var(--muted); margin-bottom: 20px; font-size: 12px; }}
    .grid {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(280px, 1fr)); gap: 16px; }}
    .tile {{ background: var(--card); border: 1px solid var(--border); border-radius: 8px;
             padding: 12px; }}
    .tile.flagged-good {{ border-color: var(--good); }}
    .tile.flagged-bad {{ border-color: var(--bad); }}
    .tile h2 {{ font-size: 13px; font-weight: 600; margin: 0 0 8px; color: var(--muted);
                font-family: ui-monospace, "SF Mono", Menlo, monospace; }}
    video {{ width: 100%; height: auto; border-radius: 4px; background: #000; display: block; }}
    audio {{ width: 100%; margin-top: 8px; }}
    .prompt {{ color: var(--muted); font-size: 12px; margin: 8px 0 4px;
                font-style: italic; }}
    .flags {{ display: flex; gap: 6px; margin-top: 8px; }}
    button {{ flex: 1; background: transparent; color: var(--fg); border: 1px solid var(--border);
              border-radius: 4px; padding: 6px 8px; cursor: pointer; font: inherit; }}
    button:hover {{ border-color: var(--muted); }}
    button.active.good {{ background: var(--good); color: #04220e; border-color: var(--good); }}
    button.active.bad  {{ background: var(--bad);  color: #2a0508; border-color: var(--bad); }}
    .actions {{ margin: 16px 0 24px; display: flex; gap: 8px; }}
    .actions button {{ flex: 0 0 auto; padding: 8px 14px; }}
  </style>
</head>
<body>
  <h1>Foley Review — {video_name}</h1>
  <div class="meta">Grid: {cols}×{rows} · Overlap: {overlap} · Duration: {duration:.2f}s · {tile_count} tiles</div>
  <div class="actions">
    <button id="download-flags">Download flagged.json</button>
    <button id="clear-flags">Clear flags</button>
  </div>
  <div class="grid" id="grid"></div>

  <script id="data" type="application/json">{data_json}</script>
  <script>
    const data = JSON.parse(document.getElementById('data').textContent);
    const grid = document.getElementById('grid');
    const flags = JSON.parse(localStorage.getItem('foley_flags') || '{{}}');

    function render() {{
      grid.innerHTML = '';
      for (const t of data.tiles) {{
        const flag = flags[t.id];
        const card = document.createElement('div');
        card.className = 'tile' + (flag === 'good' ? ' flagged-good' : flag === 'bad' ? ' flagged-bad' : '');
        card.innerHTML = `
          <h2>${{t.id}} — rect [${{t.rect.join(', ')}}]</h2>
          <video src="${{t.tile_clip}}" controls muted preload="metadata"></video>
          <audio src="${{t.foley_audio}}" controls preload="none"></audio>
          <div class="prompt">${{t.prompt ? t.prompt.replace(/</g, '&lt;') : '(no prompt recorded)'}}</div>
          <div class="flags">
            <button data-id="${{t.id}}" data-flag="good" class="${{flag === 'good' ? 'active good' : ''}}">👍 Keep</button>
            <button data-id="${{t.id}}" data-flag="bad"  class="${{flag === 'bad'  ? 'active bad'  : ''}}">👎 Re-roll</button>
          </div>
        `;
        grid.appendChild(card);
      }}
    }}
    render();

    grid.addEventListener('click', (e) => {{
      const btn = e.target.closest('button[data-id]');
      if (!btn) return;
      const id = btn.dataset.id;
      const flag = btn.dataset.flag;
      flags[id] = flags[id] === flag ? null : flag;
      if (!flags[id]) delete flags[id];
      localStorage.setItem('foley_flags', JSON.stringify(flags));
      render();
    }});

    document.getElementById('download-flags').addEventListener('click', () => {{
      const blob = new Blob([JSON.stringify({{ flags }}, null, 2)], {{ type: 'application/json' }});
      const a = document.createElement('a');
      a.href = URL.createObjectURL(blob);
      a.download = 'flagged.json';
      a.click();
    }});
    document.getElementById('clear-flags').addEventListener('click', () => {{
      if (!confirm('Clear all flags?')) return;
      for (const k of Object.keys(flags)) delete flags[k];
      localStorage.setItem('foley_flags', JSON.stringify(flags));
      render();
    }});
  </script>
</body>
</html>
"""


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Render a Foley review page from a tiles manifest.")
    p.add_argument("--manifest", type=Path, required=True, help="tiles.json with foley_audio paths.")
    p.add_argument("--out", type=Path, required=True, help="Output review.html path.")
    return p


def _relative_to(target: Path, base: Path) -> str:
    try:
        return os.path.relpath(target, base)
    except ValueError:
        return str(target)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    manifest_path = args.manifest.expanduser().resolve()
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    out_path = args.out.expanduser().resolve()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_dir = manifest_path.parent

    tiles_view: list[dict[str, Any]] = []
    for tile in manifest.get("tiles", []):
        clip_abs = (manifest_dir / tile["tile_clip"]).resolve()
        audio_rel = tile.get("foley_audio")
        audio_abs = (manifest_dir / audio_rel).resolve() if audio_rel else None
        tiles_view.append({
            "id": tile["id"],
            "rect": tile["rect"],
            "rect_norm": tile["rect_norm"],
            "tile_clip": _relative_to(clip_abs, out_path.parent),
            "foley_audio": _relative_to(audio_abs, out_path.parent) if audio_abs else "",
            "prompt": tile.get("prompt", ""),
        })

    data_json = json.dumps({"tiles": tiles_view})
    grid = manifest.get("grid", {})
    page = PAGE_TEMPLATE.format(
        video_name=html.escape(Path(manifest.get("video", "")).name),
        cols=grid.get("cols", "?"),
        rows=grid.get("rows", "?"),
        overlap=grid.get("overlap", "?"),
        duration=manifest.get("trimmed_duration") or manifest.get("duration") or 0.0,
        tile_count=len(tiles_view),
        data_json=data_json,
    )
    out_path.write_text(page, encoding="utf-8")
    print(f"wrote_review={out_path}")

    # --- universal result manifest -----------------------
    result_manifest_path = out_path.parent / "manifest.json"
    manifest = build_manifest(
        kind="foley_review",
        inputs={
            "manifest": str(manifest_path),
        },
        outputs=[
            {"path": out_path.name, "type": "file"},
        ],
        created=datetime.now(timezone.utc).isoformat(),
    )
    write_manifest(result_manifest_path, manifest)
    # -------------------------------------------------------------------------

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
