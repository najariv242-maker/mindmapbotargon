"""
renderer.py - Low-Memory, Timeout-Protected XMind Renderer for Render (512MB RAM)
- Central Topic (Level 0): 30px, Center-aligned
- Main Topics (Level 1): 24px, Center-aligned
- Sub-topics (Level 2+): 20px, Right-aligned on both sides
- Single-context rendering pipeline to prevent OOM freezes on Render Free Tier
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

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_FONTS_DIR = BASE_DIR / "fonts"

PAGE_TIMEOUT_MS = 25_000  # حداکثر ۲۵ ثانیه برای هر عملیات صفحه

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

# سایز دقیق فونت‌ها طبق دستور شما
FONT_SIZE_CENTRAL = 30
FONT_SIZE_MAIN = 24
FONT_SIZE_SUB = 20

RAINBOW_PALETTE: List[str] = [
    "#EF476F",
    "#F77F00",
    "#D97706",
    "#10B981",
    "#2563EB",
    "#7C3AED",
]

ROOT_BG = "#1E1E2E"
ROOT_BORDER = "#7C3AED"
ROOT_COLOR = "#FFFFFF"
SUBTEXT_COLOR = "#1E293B"
CANVAS_BG = "#F8FAFC"

PAD_X = 90
PAD_Y = 80
BRANCH_GAP = 72
SIBLING_GAP = 20
CHILD_GAP = 12
SUBTREE_GAP = 44


@dataclass(frozen=True)
class RenderResult:
    png_bytes: bytes
    pdf_bytes: bytes
    width: int
    height: int
    font_key: str
    font_label: str


# تشخیص عبارات انگلیسی و پرانتزها برای جلوگیری از برعکس شدن پرانتز در متن فارسی
_BIDI_TOKEN_RE = re.compile(
    r"\([A-Za-z0-9_./+:=#&*,\-–—\s]+\)"
    r"|<[A-Za-z0-9_.:/\-]+>"
    r"|[A-Za-z0-9][A-Za-z0-9_./+:=#&*\-]*(?:\s+[A-Za-z0-9_./+:=#&*\-]+)*"
)


def format_bidi_html(text: str) -> str:
    """ایزولاسیون عبارات انگلیسی و پرانتزها با LRM (&lrm;) برای جلوگیری از وارونگی پرانتز."""
    parts: List[str] = []
    last_idx = 0

    for match in _BIDI_TOKEN_RE.finditer(text):
        start, end = match.span()
        token = match.group(0)

        if not re.search(r"[A-Za-z()<>]", token):
            continue

        if start > last_idx:
            parts.append(html.escape(text[last_idx:start]))

        parts.append(
            f'<bdi class="ltr-token" dir="ltr">&lrm;{html.escape(token)}&lrm;</bdi>'
        )
        last_idx = end

    if last_idx < len(text):
        parts.append(html.escape(text[last_idx:]))

    return "".join(parts)


def _resolve_font_file(font_key: str, fonts_dir: Path) -> Optional[Path]:
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
    return (
        f"@font-face {{\n"
        f"  font-family: '{family_name}';\n"
        f"  src: url('data:{mime};base64,{b64_data}') format('{fmt}');\n"
        f"  font-weight: 100 900;\n"
        f"  font-style: normal;\n"
        f"  font-display: block;\n"
        f"}}"
    )


def build_font_face_css(
    font_key: str, fonts_dir: Optional[Path] = None
) -> Tuple[str, str]:
    target_dir = Path(fonts_dir) if fonts_dir else DEFAULT_FONTS_DIR
    cfg = FONT_CONFIG.get(font_key, FONT_CONFIG["font_a"])
    family_name = str(cfg["family_name"])

    resolved = _resolve_font_file(font_key, target_dir)
    if resolved is not None:
        return family_name, _encode_font_css_cached(str(resolved), family_name)

    return family_name, ""


def _count_descendants(node: Node) -> int:
    return sum(1 + _count_descendants(c) for c in node.children)


def _split_balanced(children: List[Node]) -> Tuple[List[Node], List[Node]]:
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


def _node_style(level: int, color: str) -> str:
    if level == 0:
        return (
            f"background:{ROOT_BG};border:3px solid {ROOT_BORDER};"
            f"color:{ROOT_COLOR};border-radius:26px;"
            "padding:18px 34px;"
            f"font-size:{FONT_SIZE_CENTRAL}px;font-weight:700;"
            "text-align:center;justify-content:center;"
            "box-shadow:0 6px 24px rgba(124,58,237,0.28);"
        )
    if level == 1:
        return (
            f"background:{color};color:#FFFFFF;"
            "border-radius:20px;padding:12px 26px;"
            f"font-size:{FONT_SIZE_MAIN}px;font-weight:600;"
            "text-align:center;justify-content:center;"
            "text-shadow:0 1px 2px rgba(0,0,0,0.18);"
            "box-shadow:0 3px 12px rgba(0,0,0,0.14);"
        )
    return (
        "background:transparent;"
        f"color:{SUBTEXT_COLOR};"
        "padding:6px 14px 9px 14px;"
        f"font-size:{FONT_SIZE_SUB}px;font-weight:500;"
        "text-align:right;justify-content:flex-start;"
    )


def _build_branch_html(node: Node, color: str, side: str) -> str:
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
      const yShift = Math.max(-pr.h * 0.22, Math.min(pr.h * 0.22, (cr.cy - pr.cy) * 0.20));
      const y1 = pr.cy + yShift;
      const x2 = (side === 'right') ? cr.x : cr.right;
      const y2 = cr.cy;

      pathEl.setAttribute('d', makeTaperedPath(x1, y1, x2, y2, 9, 3.2));
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
      pathEl.setAttribute('stroke-width', '2.6');
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
  font-family: '{font_family}', 'Tahoma', sans-serif;
  overflow: visible;
  width: max-content;
  height: max-content;
  -webkit-font-smoothing: antialiased;
  text-rendering: geometricPrecision;
}}

#page-root {{
  direction: ltr;
  display: inline-block;
  padding: {pad_y}px {pad_x}px;
  background: {canvas_bg};
  width: max-content;
  height: max-content;
}}

#mindmap-wrapper {{
  direction: ltr;
  display: inline-flex;
  flex-direction: row;
  align-items: center;
  gap: {branch_gap}px;
  position: relative;
  width: max-content;
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

.node {{
  position: relative;
  display: flex;
  align-items: center;
  direction: rtl;
  unicode-bidi: isolate;
  white-space: normal;
  word-break: normal;
  overflow-wrap: break-word;
  line-height: 1.55;
  z-index: 1;
}}

.node.level-0    {{ max-width: 400px; min-width: 180px; }}
.node.level-1    {{ max-width: 320px; min-width: 140px; }}
.node.level-2    {{ max-width: 380px; min-width: 80px;  }}
.node.level-deep {{ max-width: 450px; min-width: 80px;  }}

.node-inner {{
  direction: rtl;
  unicode-bidi: isolate;
  display: block;
  width: 100%;
}}

/* وسط‌چین شدن تاپیک مرکزی (۳۰) و تاپیک‌های اصلی (۲۴) */
.node.level-0 .node-inner,
.node.level-1 .node-inner {{
  text-align: center;
}}

/* راست‌چین شدن تمام زیرشاخه‌ها (۲۰) در هر دو سمت چپ و راست */
.node.level-2 .node-inner,
.node.level-deep .node-inner {{
  text-align: right;
}}

/* حفظ جریان طبیعی عبارات انگلیسی و پرانتزها در دل متن فارسی */
.ltr-token {{
  direction: ltr;
  unicode-bidi: isolate;
  display: inline;
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
        pad_x=PAD_X,
        pad_y=PAD_Y,
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


class BrowserManager:
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
        async with cls._get_lock():
            target_dir = Path(fonts_dir) if fonts_dir else DEFAULT_FONTS_DIR
            for f_key in FONT_CONFIG:
                try:
                    build_font_face_css(f_key, fonts_dir=target_dir)
                except Exception as exc:
                    logger.warning("Could not pre-warm font '%s': %s", f_key, exc)

            if cls._browser is None or not cls._browser.is_connected():
                await cls._force_restart_browser()

    @classmethod
    async def _force_restart_browser(cls) -> None:
        from playwright.async_api import async_playwright

        if cls._browser is not None:
            try:
                await cls._browser.close()
            except Exception:
                pass
            cls._browser = None

        if cls._playwright is not None:
            try:
                await cls._playwright.stop()
            except Exception:
                pass
            cls._playwright = None

        cls._playwright = await async_playwright().start()
        cls._browser = await cls._playwright.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
                "--disable-software-rasterizer",
                "--disable-extensions",
                "--font-render-hinting=none",
                "--force-color-profile=srgb",
                "--js-flags=--max-old-space-size=256",
            ],
        )
        logger.info("Headless Chromium launched with low-memory flags.")

    @classmethod
    async def new_context(
        cls,
        scale: float = 1.0,
        vw: int = 1400,
        vh: int = 900,
    ):
        async with cls._get_lock():
            if cls._browser is None or not cls._browser.is_connected():
                await cls._force_restart_browser()
            try:
                return await cls._browser.new_context(
                    viewport={"width": vw, "height": vh},
                    device_scale_factor=scale,
                )
            except Exception:
                await cls._force_restart_browser()
                return await cls._browser.new_context(
                    viewport={"width": vw, "height": vh},
                    device_scale_factor=scale,
                )

    @classmethod
    async def shutdown(cls) -> None:
        async with cls._get_lock():
            if cls._browser is not None:
                try:
                    await cls._browser.close()
                except Exception:
                    pass
                cls._browser = None
            if cls._playwright is not None:
                try:
                    await cls._playwright.stop()
                except Exception:
                    pass
                cls._playwright = None


async def render_mindmap(
    root: Node,
    font_key: str = "font_a",
    fonts_dir: Optional[Path] = None,
) -> RenderResult:
    """
    Single-Pass Low-Memory Render Pipeline:
    1. Opens a lightweight viewport (1400x900) with scale=1.75 (fits easily in 512MB RAM).
    2. Loads HTML once, waits for fonts (with timeout), draws SVG, and measures exact size.
    3. Resizes viewport to exact bounding box, captures high-res PNG, then exports Vector PDF.
    """
    normalized_key = font_key if font_key in FONT_CONFIG else "font_a"
    font_label = str(FONT_CONFIG[normalized_key]["button_label"])

    html_content = build_mindmap_html(
        root=root, font_key=normalized_key, fonts_dir=fonts_dir
    )

    # انتخاب مقیاس هوشمند بر اساس تعداد گره‌ها برای جلوگیری از پر شدن رم ۵۱۲ مگابایتی Render
    total_nodes = root.count_nodes()
    scale = 2.0 if total_nodes <= 35 else 1.6

    ctx = await BrowserManager.new_context(scale=scale, vw=1400, vh=900)
    try:
        page = await ctx.new_page()
        page.set_default_timeout(PAGE_TIMEOUT_MS)

        await page.set_content(
            html_content,
            wait_until="domcontentloaded",
            timeout=PAGE_TIMEOUT_MS,
        )

        # انتظار برای فونت‌ها با سقف زمانی مشخص (عدم فریز در صورت تاخیر فونت)
        try:
            await asyncio.wait_for(
                page.evaluate("() => document.fonts ? document.fonts.ready : Promise.resolve()"),
                timeout=8.0,
            )
        except Exception:
            logger.warning("Font ready wait timed out; continuing render.")

        # ترسیم اولیه و اندازه‌گیری دقیق کل نقشه
        dims = await page.evaluate(
            """() => new Promise(resolve => {
                window.__drawMindMap();
                requestAnimationFrame(() => {
                    const el = document.getElementById('page-root');
                    const r  = el.getBoundingClientRect();
                    resolve({
                        w: Math.ceil(Math.max(r.width, el.scrollWidth)),
                        h: Math.ceil(Math.max(r.height, el.scrollHeight))
                    });
                });
            })"""
        )

        w = max(600, min(7000, int(dims["w"])))
        h = max(400, min(7000, int(dims["h"])))

        await page.set_viewport_size({"width": w, "height": h})
        await page.evaluate(
            """() => new Promise(resolve => {
                window.__drawMindMap();
                requestAnimationFrame(resolve);
            })"""
        )

        # ۱. دریافت تصویر PNG باکیفیت
        locator = page.locator("#page-root")
        png_bytes = await locator.screenshot(type="png", timeout=PAGE_TIMEOUT_MS)

        # ۲. دریافت فایل Vector PDF در همان صفحه (بدون باز کردن مجدد مرورگر)
        await page.evaluate(
            f"""() => {{
                const s = document.createElement('style');
                s.textContent =
                    '@page {{ size: {w}px {h}px; margin: 0; }} ' +
                    'html, body {{ width: {w}px !important; height: {h}px !important; ' +
                    'margin: 0 !important; padding: 0 !important; overflow: hidden !important; ' +
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
    finally:
        try:
            await ctx.close()
        except Exception:
            pass
