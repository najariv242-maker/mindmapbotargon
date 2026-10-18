"""
renderer.py - Production-Ready XMind-Style HTML5/CSS3 + SVG Mind Map Renderer
=============================================================================
Architectural Highlights:
1. Configurable `FONT_CONFIG` dictionary at the top of the module with
   `button_label` keys for one-line Telegram inline button customization.
2. Automatic Base64 Data URI + `file://` URI `@font-face` injection with
   `@functools.lru_cache` in-memory caching (supports `.ttf`, `.woff2`, `.otf`).
3. Singleton `BrowserManager` with `asyncio.Lock` and auto-reconnection so
   Headless Chromium is launched once and reused across all renders.
4. Hybrid Layout & Vector Geometry:
   - Physical LTR Flexbox layout prevents overlapping nodes on multi-line text.
   - Strict RTL & BiDi token isolation (`<bdi dir="ltr" class="ltr-token">`)
     inside nodes for mixed Persian/English/technical strings.
   - Tapered Bezier ribbons (Level 0 -> Level 1) + Unified Continuous SVG
     Bezier-to-Underline paths (Level 1 -> Level 2+) with zero seams at 4000% zoom.
5. In-Memory (`bytes`) output generation for zero-disk-I/O operation.
"""

from __future__ import annotations

import asyncio
import base64
import functools
import html
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from parser import Node

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Directory & Font Configuration (Single Source of Truth for Bot & Renderer)
# ---------------------------------------------------------------------------

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_FONTS_DIR = BASE_DIR / "fonts"

FONT_CONFIG: Dict[str, Dict[str, object]] = {
    "font_a": {
        "id": "font_a",
        "button_label": "🅰️ فونت فرهنگ ۲ (Farhang2)",
        "family_name": "MindMapCustomFontA",
        "filename": "Font_Option_1.ttf",
        "fallback_woff2": "Font_Option_1.woff2",
        "aliases": [
            "Farhang2-Regular.ttf",
            "Farhang2.ttf",
            "Farhang2.woff2",
            "Font_A.ttf",
            "Font_A.woff2",
        ],
    },
    "font_b": {
        "id": "font_b",
        "button_label": "🅱️ فونت پینار / وزیرمتن (Pinar / Vazirmatn)",
        "family_name": "MindMapCustomFontB",
        "filename": "Font_Option_2.ttf",
        "fallback_woff2": "Font_Option_2.woff2",
        "aliases": [
            "Pinar-Regular.ttf",
            "Pinar.ttf",
            "Pinar.woff2",
            "Vazirmatn-Regular.ttf",
            "Vazirmatn.ttf",
            "Vazirmatn.woff2",
            "Font_B.ttf",
            "Font_B.woff2",
        ],
    },
}

# ---------------------------------------------------------------------------
# Color Palette & Geometry Constants
# ---------------------------------------------------------------------------

RAINBOW_PALETTE: List[str] = [
    "#FF6B6B",  # Coral Red
    "#FF922B",  # Vibrant Orange
    "#F59F00",  # High-Contrast Golden Amber
    "#51CF66",  # Emerald Green
    "#339AF0",  # Royal Blue
    "#845EF7",  # Deep Violet
]

ROOT_BG = "#1E1E2E"
ROOT_BORDER = "#845EF7"
ROOT_COLOR = "#F8F9FA"
SUBTEXT_COLOR = "#1E1E2E"
CANVAS_BG = "#F7F8FC"

PAD = 80
BRANCH_GAP = 64
SIBLING_GAP = 16
CHILD_GAP = 10
SUBTREE_GAP = 38


@dataclass(frozen=True)
class RenderResult:
    """In-memory container for rendered Mind Map artifacts and dimensions."""

    png_bytes: bytes
    pdf_bytes: bytes
    width: int
    height: int
    font_key: str
    font_label: str


# ---------------------------------------------------------------------------
# BiDi Token Isolator for Mixed Persian + English + Technical Strings
# ---------------------------------------------------------------------------

_ASCII_ATOM = (
    r"(?:"
    r"<[A-Za-z0-9_.:/-]+>"  # XML/HTML tags like <outline>
    r"|\([A-Za-z0-9_./+:=#&*\s-]+\)"  # Pure ASCII parentheticals like (Parser)
    r"|[0-9۰-۹]+(?:\.[0-9۰-۹]+)?(?:px|pt|em|rem|vh|vw|ms|s|%|x)\b"  # Units like 30px
    r"|[.#@/+-\~]?[A-Za-z][A-Za-z0-9_./+:=#&*-]*"  # English words / extensions (.opml)
    r")"
)

_LTR_SEGMENT_RE = re.compile(
    rf"{_ASCII_ATOM}(?:\s+(?:[+#&/-]+\s+)?{_ASCII_ATOM})*"
)


def format_bidi_html(text: str) -> str:
    """
    Escape HTML entities while wrapping embedded English/technical tokens in
    `<bdi class="ltr-token" dir="ltr">...</bdi>` using standard Unicode isolation
    so mixed Persian and English text renders in natural RTL reading order.
    """
    parts: List[str] = []
    last_idx = 0
    for match in _LTR_SEGMENT_RE.finditer(text):
        start, end = match.span()
        if start > last_idx:
            parts.append(html.escape(text[last_idx:start]))
        token = match.group(0)
        parts.append(
            f'<bdi class="ltr-token" dir="ltr">{html.escape(token)}</bdi>'
        )
        last_idx = end
    if last_idx < len(text):
        parts.append(html.escape(text[last_idx:]))
    return "".join(parts)


# ---------------------------------------------------------------------------
# Smart Font Resolver & Base64 Data URI Injector (Cached in RAM)
# ---------------------------------------------------------------------------


def _resolve_font_file(font_key: str, fonts_dir: Path) -> Optional[Path]:
    """Locate the target font file (.ttf, .woff2, .otf) in `fonts_dir`."""
    cfg = FONT_CONFIG.get(font_key, FONT_CONFIG["font_a"])
    search_dirs = [fonts_dir, BASE_DIR / "font", DEFAULT_FONTS_DIR]

    candidate_names: List[str] = [
        str(cfg["filename"]),
        str(cfg["fallback_woff2"]),
    ]
    aliases = cfg.get("aliases", [])
    if isinstance(aliases, list):
        candidate_names.extend(str(a) for a in aliases)

    for directory in search_dirs:
        if not directory.is_dir():
            continue
        for name in candidate_names:
            candidate_path = directory / name
            if candidate_path.is_file():
                return candidate_path.resolve()

    # Fallback: auto-discover any .woff2 / .ttf / .otf files inside the folder
    for directory in search_dirs:
        if not directory.is_dir():
            continue
        all_fonts = sorted(
            p
            for p in directory.iterdir()
            if p.is_file() and p.suffix.lower() in (".woff2", ".ttf", ".otf")
        )
        if all_fonts:
            idx = 0 if font_key == "font_a" else min(1, len(all_fonts) - 1)
            return all_fonts[idx].resolve()

    return None


@functools.lru_cache(maxsize=8)
def _encode_font_css_cached(resolved_path_str: str, family_name: str) -> str:
    """
    Read a font file from disk once, convert it to a Base64 Data URI + file:// URI,
    and cache the complete `@font-face` CSS block in memory.
    """
    font_path = Path(resolved_path_str)
    ext = font_path.suffix.lower().lstrip(".")
    if ext == "woff2":
        fmt = "woff2"
        mime = "font/woff2"
    elif ext == "otf":
        fmt = "opentype"
        mime = "font/otf"
    else:
        fmt = "truetype"
        mime = "font/ttf"

    b64_data = base64.b64encode(font_path.read_bytes()).decode("ascii")
    file_uri = font_path.as_uri()

    return (
        f"@font-face {{\n"
        f"  font-family: '{family_name}';\n"
        f"  src: url('data:{mime};base64,{b64_data}') format('{fmt}'),\n"
        f"       url('{file_uri}') format('{fmt}');\n"
        f"  font-weight: 100 900;\n"
        f"  font-style: normal;\n"
        f"  font-display: block;\n"
        f"}}"
    )


def build_font_face_css(
    font_key: str, fonts_dir: Optional[Path] = None
) -> Tuple[str, str]:
    """
    Return `(family_name, font_face_css)` for the requested `font_key`.
    Embeds the font as Base64 Data URI + local `file://` URI fallback.
    """
    target_dir = Path(fonts_dir) if fonts_dir else DEFAULT_FONTS_DIR
    cfg = FONT_CONFIG.get(font_key, FONT_CONFIG["font_a"])
    family_name = str(cfg["family_name"])

    resolved = _resolve_font_file(font_key, target_dir)
    if resolved is not None:
        return family_name, _encode_font_css_cached(str(resolved), family_name)

    logger.warning(
        "Font file for '%s' not found in %s; falling back to system Persian fonts.",
        font_key,
        target_dir,
    )
    fallback_uri = (target_dir / str(cfg["filename"])).resolve().as_uri()
    css = (
        f"@font-face {{\n"
        f"  font-family: '{family_name}';\n"
        f"  src: url('{fallback_uri}') format('truetype');\n"
        f"  font-weight: 100 900;\n"
        f"  font-style: normal;\n"
        f"}}"
    )
    return family_name, css


# ---------------------------------------------------------------------------
# Weight-Balanced Tree Split
# ---------------------------------------------------------------------------


def _count_descendants(node: Node) -> int:
    """Count all descendants in the subtree of `node`."""
    return sum(1 + _count_descendants(c) for c in node.children)


def _split_balanced(children: List[Node]) -> Tuple[List[Node], List[Node]]:
    """
    Distribute Level-1 branches between right and left subtrees to minimize
    the difference in total descendant weight while preserving natural top-down order.
    """
    n = len(children)
    if n == 0:
        return [], []
    if n == 1:
        return children[:], []

    weights = [1 + _count_descendants(c) for c in children]
    total_w = sum(weights)
    target = total_w / 2.0

    best_k = 1
    best_diff = float("inf")
    running_w = 0
    for k in range(1, n):
        running_w += weights[k - 1]
        diff = abs(running_w - target)
        if diff < best_diff:
            best_diff = diff
            best_k = k

    return children[:best_k], children[best_k:]


# ---------------------------------------------------------------------------
# HTML, CSS & SVG Builders
# ---------------------------------------------------------------------------


def _node_style(level: int, color: str) -> str:
    """Generate inline CSS styles for a node based on its depth level."""
    if level == 0:
        return (
            f"background:{ROOT_BG};border:2.5px solid {ROOT_BORDER};"
            f"color:{ROOT_COLOR};border-radius:28px;"
            "padding:14px 28px;"
            "font-size:30px;font-weight:700;"
            "box-shadow:0 4px 20px rgba(132,94,247,0.30);"
        )
    if level == 1:
        return (
            f"background:{color};color:#FFFFFF;"
            "border-radius:22px;padding:9px 22px;"
            "font-size:24px;font-weight:600;"
            "box-shadow:0 2px 10px rgba(0,0,0,0.14);"
        )
    # Level 2+: Seamless SVG underline replaces CSS border-bottom
    return (
        "background:transparent;"
        f"color:{SUBTEXT_COLOR};"
        "padding:4px 10px 7px 10px;"
        "font-size:18px;font-weight:500;"
    )


def _build_branch_html(node: Node, color: str, side: str) -> str:
    """Recursively build nested Flexbox HTML for a branch and its children."""
    level = node.level
    n_style = _node_style(level, color)
    safe_bidi_html = format_bidi_html(node.text)
    depth_cls = f"level-{level}" if level <= 2 else f"level-{level} level-deep"

    node_div = (
        f'<div class="node {depth_cls}" '
        f'data-level="{level}" data-color="{color}" data-side="{side}" '
        f'style="{n_style}">'
        f'<div class="node-inner" dir="rtl">{safe_bidi_html}</div>'
        f"</div>"
    )

    if node.children:
        ch_html = "\n".join(
            _build_branch_html(c, color, side) for c in node.children
        )
        children_div = f'<div class="branch-children">{ch_html}</div>'
    else:
        children_div = '<div class="branch-children"></div>'

    return (
        f'<div class="branch-group" data-color="{color}" data-side="{side}">'
        f"{node_div}{children_div}"
        f"</div>"
    )


def _build_tree_html(branches: List[Node], side: str, color_offset: int) -> str:
    """Build HTML for all top-level branches on one side (right or left)."""
    return "\n".join(
        _build_branch_html(
            b, RAINBOW_PALETTE[(color_offset + i) % len(RAINBOW_PALETTE)], side
        )
        for i, b in enumerate(branches)
    )


_JS_DRAW = r"""
window.__drawMindMap = function () {
  const existing = document.getElementById('svg-canvas');
  if (existing) existing.remove();

  const wrapper = document.getElementById('mindmap-wrapper');
  const wRect = wrapper.getBoundingClientRect();

  const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  svg.id = 'svg-canvas';
  svg.setAttribute('width', Math.ceil(wRect.width));
  svg.setAttribute('height', Math.ceil(wRect.height));
  svg.style.cssText =
    'position:absolute;top:0;left:0;overflow:visible;' +
    'pointer-events:none;z-index:0;';
  wrapper.insertBefore(svg, wrapper.firstChild);

  function toLocal(r) {
    return {
      x: r.left - wRect.left,
      y: r.top - wRect.top,
      w: r.width,
      h: r.height,
      cx: r.left - wRect.left + r.width / 2,
      cy: r.top - wRect.top + r.height / 2,
      right: r.right - wRect.left,
      bottom: r.bottom - wRect.top,
    };
  }

  function getParentAnchorY(pr, parentLevel) {
    return (parentLevel >= 2) ? pr.bottom : pr.cy;
  }

  /* Tapered ribbon (Level 0 -> Level 1) with stabilized chord mid-normals */
  function makeTaperedPath(x1, y1, x2, y2, w1, w2) {
    const dx = x2 - x1;
    const cx1 = x1 + dx * 0.5, cy1 = y1;
    const cx2 = x2 - dx * 0.5, cy2 = y2;

    let chx = x2 - x1, chy = y2 - y1;
    const chl = Math.hypot(chx, chy) || 1;
    chx /= chl; chy /= chl;
    const nmx = -chy, nmy = chx;

    let t0x = cx1 - x1, t0y = cy1 - y1;
    const l0 = Math.hypot(t0x, t0y) || 1;
    t0x /= l0; t0y /= l0;
    const n0x = -t0y, n0y = t0x;

    let t1x = x2 - cx2, t1y = y2 - cy2;
    const l1 = Math.hypot(t1x, t1y) || 1;
    t1x /= l1; t1y /= l1;
    const n1x = -t1y, n1y = t1x;

    const h0 = w1 / 2;
    const hm = (w1 + w2) / 4;
    const h1 = w2 / 2;

    const ox1  = x1  + n0x * h0, oy1  = y1  + n0y * h0;
    const ocx1 = cx1 + nmx * hm, ocy1 = cy1 + nmy * hm;
    const ocx2 = cx2 + nmx * hm, ocy2 = cy2 + nmy * hm;
    const ox2  = x2  + n1x * h1, oy2  = y2  + n1y * h1;

    const ix1  = x1  - n0x * h0, iy1  = y1  - n0y * h0;
    const icx1 = cx1 - nmx * hm, icy1 = cy1 - nmy * hm;
    const icx2 = cx2 - nmx * hm, icy2 = cy2 - nmy * hm;
    const ix2  = x2  - n1x * h1, iy2  = y2  - n1y * h1;

    return (
      `M ${ox1},${oy1} ` +
      `C ${ocx1},${ocy1} ${ocx2},${ocy2} ${ox2},${oy2} ` +
      `L ${ix2},${iy2} ` +
      `C ${icx2},${icy2} ${icx1},${icy1} ${ix1},${iy1} Z`
    );
  }

  /* Unified Continuous Path (Level 1 -> Level 2+): Bezier + Horizontal Underline */
  function makeContinuousUnderlinePath(x1, y1, x2_inner, y2, x2_outer) {
    const dx = x2_inner - x1;
    const cx1 = x1 + dx * 0.5;
    const cx2 = x2_inner - dx * 0.5;
    return (
      `M ${x1},${y1} ` +
      `C ${cx1},${y1} ${cx2},${y2} ${x2_inner},${y2} ` +
      `L ${x2_outer},${y2}`
    );
  }

  function drawCurve(parentEl, childEl, side, connectionLevel, color) {
    const pr = toLocal(parentEl.getBoundingClientRect());
    const cr = toLocal(childEl.getBoundingClientRect());

    const parentLevel = parseInt(parentEl.dataset.level || '0', 10);
    const childLevel  = parseInt(childEl.dataset.level  || '0', 10);

    const pathEl = document.createElementNS('http://www.w3.org/2000/svg', 'path');

    if (connectionLevel === 0) {
      const x1 = (side === 'right') ? pr.right : pr.x;
      const y1 = pr.cy;
      const x2 = (side === 'right') ? cr.x : cr.right;
      const y2 = cr.cy;

      pathEl.setAttribute('d', makeTaperedPath(x1, y1, x2, y2, 8, 3));
      pathEl.setAttribute('fill', color);
      pathEl.setAttribute('stroke', 'none');
    } else {
      const x1 = (side === 'right') ? pr.right : pr.x;
      const y1 = getParentAnchorY(pr, parentLevel);
      const x2_inner = (side === 'right') ? cr.x : cr.right;
      const x2_outer = (side === 'right') ? cr.right : cr.x;
      const y2 = (childLevel >= 2) ? cr.bottom : cr.cy;

      pathEl.setAttribute('d', makeContinuousUnderlinePath(x1, y1, x2_inner, y2, x2_outer));
      pathEl.setAttribute('fill', 'none');
      pathEl.setAttribute('stroke', color);
      pathEl.setAttribute('stroke-width', '2.5');
      pathEl.setAttribute('stroke-linecap', 'round');
      pathEl.setAttribute('stroke-linejoin', 'round');
    }

    svg.appendChild(pathEl);
  }

  function traverse(bgEl, parentNodeEl, side, connectionLevel) {
    const nodeEl = bgEl.querySelector(':scope > .node');
    const color  = bgEl.dataset.color;
    if (parentNodeEl && nodeEl) {
      drawCurve(parentNodeEl, nodeEl, side, connectionLevel, color);
    }
    const childrenEl = bgEl.querySelector(':scope > .branch-children');
    if (childrenEl && nodeEl) {
      childrenEl.querySelectorAll(':scope > .branch-group').forEach(child => {
        traverse(child, nodeEl, side, connectionLevel + 1);
      });
    }
  }

  const rootEl = document.querySelector('.central-node');
  document.querySelectorAll('.right-tree > .branch-group').forEach(bg => {
    traverse(bg, rootEl, 'right', 0);
  });
  document.querySelectorAll('.left-tree > .branch-group').forEach(bg => {
    traverse(bg, rootEl, 'left', 0);
  });
};
"""

_HTML_TEMPLATE = """\
<!DOCTYPE html>
<html lang="fa">
<head>
<meta charset="UTF-8">
<style>
{font_face_css}

*, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}

html, body {{
  direction: ltr;
  background: {canvas_bg};
  font-family: '{font_family}', 'Vazirmatn', 'Tahoma', 'Arial Unicode MS', sans-serif;
  overflow: visible;
  -webkit-font-smoothing: antialiased;
  text-rendering: geometricPrecision;
}}

#page-root {{
  direction: ltr;
  display: inline-block;
  padding: {pad}px;
  background: {canvas_bg};
}}

#mindmap-wrapper {{
  direction: ltr;
  display: inline-flex;
  flex-direction: row;
  align-items: center;
  gap: {branch_gap}px;
  position: relative;
}}

.tree {{
  direction: ltr;
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  justify-content: center;
  gap: {sibling_gap}px;
}}
.left-tree {{ align-items: flex-end; }}

.branch-group {{
  direction: ltr;
  display: flex;
  flex-direction: row;
  align-items: center;
  gap: {subtree_gap}px;
}}
.left-tree .branch-group {{
  flex-direction: row-reverse;
}}

.branch-children {{
  direction: ltr;
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  justify-content: center;
  gap: {child_gap}px;
}}
.left-tree .branch-children {{
  align-items: flex-end;
}}

/* Strict RTL & BiDi isolation inside nodes */
.node {{
  position: relative;
  display: flex;
  align-items: center;
  justify-content: center;
  direction: rtl;
  text-align: right;
  unicode-bidi: isolate;
  white-space: normal;
  word-break: break-word;
  overflow-wrap: break-word;
  line-height: 1.55;
  z-index: 1;
}}

/* Depth-Aware Smart Compact Widths */
.node.level-0    {{ max-width: 360px; min-width: 130px; }}
.node.level-1    {{ max-width: 260px; min-width: 95px;  }}
.node.level-2    {{ max-width: 320px; min-width: 65px;  }}
.node.level-deep {{ max-width: 400px; min-width: 65px;  }}

.node-inner {{
  direction: rtl;
  text-align: right;
  unicode-bidi: isolate;
  display: inline-block;
  max-width: 100%;
}}

.ltr-token {{
  direction: ltr;
  unicode-bidi: isolate;
  display: inline-block;
}}

#svg-canvas {{
  position: absolute;
  top: 0;
  left: 0;
  overflow: visible;
  pointer-events: none;
  z-index: 0;
}}
</style>
</head>
<body>
<div id="page-root">
  <div id="mindmap-wrapper">

{left_tree_html}

    <div class="central-node node level-0"
         data-level="0"
         style="{root_style}">
      <div class="node-inner" dir="rtl">{root_text}</div>
    </div>

    <div class="right-tree tree">
{right_html}
    </div>

  </div>
</div>
<script>
{js_draw}
</script>
</body>
</html>
"""


def build_mindmap_html(
    root: Node,
    font_key: str = "font_a",
    fonts_dir: Optional[Path] = None,
) -> str:
    """Build the self-contained HTML5/CSS3/SVG document with Base64 embedded font."""
    root.normalize_levels(0)
    font_family, font_face_css = build_font_face_css(font_key, fonts_dir=fonts_dir)

    right_ch, left_ch = _split_balanced(root.children)
    right_html = _build_tree_html(right_ch, "right", 0)
    left_html = _build_tree_html(left_ch, "left", len(right_ch))

    if left_ch:
        left_tree_html = f'    <div class="left-tree tree">\n{left_html}\n    </div>'
    else:
        left_tree_html = ""

    return _HTML_TEMPLATE.format(
        font_face_css=font_face_css,
        font_family=font_family,
        canvas_bg=CANVAS_BG,
        pad=PAD,
        branch_gap=BRANCH_GAP,
        sibling_gap=SIBLING_GAP,
        child_gap=CHILD_GAP,
        subtree_gap=SUBTREE_GAP,
        root_style=_node_style(0, ROOT_BORDER),
        root_text=format_bidi_html(root.text),
        right_html=right_html,
        left_tree_html=left_tree_html,
        js_draw=_JS_DRAW,
    )


# ---------------------------------------------------------------------------
# Singleton Playwright Browser Manager
# ---------------------------------------------------------------------------


class BrowserManager:
    """Thread-safe / Async-safe Singleton manager for Headless Chromium."""

    _playwright = None
    _browser = None
    _lock: Optional[asyncio.Lock] = None

    @classmethod
    def _get_lock(cls) -> asyncio.Lock:
        if cls._lock is None:
            cls._lock = asyncio.Lock()
        return cls._lock

    @classmethod
    async def initialize(cls, fonts_dir: Optional[Path] = None) -> None:
        """Start Playwright Chromium and pre-warm the Base64 font LRU cache."""
        async with cls._get_lock():
            target_dir = Path(fonts_dir) if fonts_dir else DEFAULT_FONTS_DIR
            for f_key in FONT_CONFIG:
                try:
                    build_font_face_css(f_key, fonts_dir=target_dir)
                except Exception as exc:
                    logger.warning("Could not pre-warm font '%s': %s", f_key, exc)

            if cls._browser is None or not cls._browser.is_connected():
                from playwright.async_api import async_playwright

                if cls._playwright is not None:
                    try:
                        await cls._playwright.stop()
                    except Exception:
                        pass

                cls._playwright = await async_playwright().start()
                cls._browser = await cls._playwright.chromium.launch(
                    headless=True,
                    args=[
                        "--no-sandbox",
                        "--disable-setuid-sandbox",
                        "--disable-dev-shm-usage",
                        "--disable-gpu",
                        "--font-render-hinting=none",
                        "--force-color-profile=srgb",
                    ],
                )
                logger.info("Headless Chromium initialized and font cache warmed.")

    @classmethod
    async def new_context(
        cls,
        scale: float = 1.0,
        vw: int = 6000,
        vh: int = 4000,
    ):
        """Create a fresh isolated BrowserContext, auto-reconnecting if needed."""
        if cls._browser is None or not cls._browser.is_connected():
            await cls.initialize()
        return await cls._browser.new_context(
            viewport={"width": vw, "height": vh},
            device_scale_factor=scale,
        )

    @classmethod
    async def shutdown(cls) -> None:
        """Gracefully close the Chromium browser and stop Playwright."""
        async with cls._get_lock():
            if cls._browser is not None:
                try:
                    await cls._browser.close()
                except Exception as exc:
                    logger.debug("Error closing browser: %s", exc)
                cls._browser = None
            if cls._playwright is not None:
                try:
                    await cls._playwright.stop()
                except Exception as exc:
                    logger.debug("Error stopping playwright: %s", exc)
                cls._playwright = None


# ---------------------------------------------------------------------------
# High-DPI PNG & Vector PDF Rendering Pipeline (In-Memory)
# ---------------------------------------------------------------------------


async def _load_and_draw(page, html_content: str) -> None:
    """Load HTML directly in memory, wait for custom fonts, and draw SVG connectors."""
    await page.set_content(html_content, wait_until="domcontentloaded")
    await page.evaluate("() => document.fonts ? document.fonts.ready : Promise.resolve()")
    await page.evaluate(
        """() => new Promise(resolve => {
            window.__drawMindMap();
            requestAnimationFrame(() => requestAnimationFrame(resolve));
        })"""
    )


async def _measure_and_export_pdf(
    html_content: str,
) -> Tuple[int, int, bytes]:
    """Measure exact DOM bounding box and generate single-page Vector PDF in RAM."""
    ctx = await BrowserManager.new_context(scale=1.0, vw=8000, vh=5000)
    try:
        page = await ctx.new_page()
        await _load_and_draw(page, html_content)

        dims = await page.evaluate(
            """() => {
                const el = document.getElementById('page-root');
                const r  = el.getBoundingClientRect();
                return { w: Math.ceil(r.width), h: Math.ceil(r.height) };
            }"""
        )
        w = max(400, int(dims["w"]))
        h = max(300, int(dims["h"]))

        await page.set_viewport_size({"width": w, "height": h})
        await page.evaluate("() => window.__drawMindMap && window.__drawMindMap()")

        await page.evaluate(
            f"""() => {{
                const s = document.createElement('style');
                s.textContent =
                    '@page {{ size: {w}px {h}px; margin: 0; }} ' +
                    'html, body {{ width: {w}px; height: {h}px; overflow: hidden; ' +
                    '-webkit-print-color-adjust: exact !important; ' +
                    'print-color-adjust: exact !important; }}';
                document.head.appendChild(s);
            }}"""
        )

        pdf_bytes = await page.pdf(
            width=f"{w}px",
            height=f"{h}px",
            print_background=True,
            page_ranges="1",
            margin={"top": "0px", "right": "0px", "bottom": "0px", "left": "0px"},
        )
        return w, h, pdf_bytes
    finally:
        await ctx.close()


async def _export_hires_png(
    html_content: str,
    w: int,
    h: int,
    scale: float,
) -> bytes:
    """Capture high-DPI PNG screenshot directly into memory."""
    ctx = await BrowserManager.new_context(scale=scale, vw=w, vh=h)
    try:
        page = await ctx.new_page()
        await _load_and_draw(page, html_content)
        locator = page.locator("#page-root")
        return await locator.screenshot(type="png")
    finally:
        await ctx.close()


async def render_mindmap(
    root: Node,
    font_key: str = "font_a",
    fonts_dir: Optional[Path] = None,
) -> RenderResult:
    """
    Render `root` into high-resolution PNG bytes and vector PDF bytes in memory.

    Automatically chooses the highest safe DPI scale factor (up to 3.0x) that
    fits within Chromium's 15,000px GPU texture limit.
    """
    normalized_key = font_key if font_key in FONT_CONFIG else "font_a"
    font_label = str(FONT_CONFIG[normalized_key]["button_label"])

    html_content = build_mindmap_html(
        root=root, font_key=normalized_key, fonts_dir=fonts_dir
    )

    w, h, pdf_bytes = await _measure_and_export_pdf(html_content)

    scale = 1.0
    for candidate in (3.0, 2.0, 1.5, 1.0):
        if max(w, h) * candidate <= 15000:
            scale = candidate
            break

    png_bytes = await _export_hires_png(html_content, w, h, scale)
    px_w = round(w * scale)
    px_h = round(h * scale)

    return RenderResult(
        png_bytes=png_bytes,
        pdf_bytes=pdf_bytes,
        width=px_w,
        height=px_h,
        font_key=normalized_key,
        font_label=font_label,
    )