"""
renderer.py - Xmind Official Elbow & Rainbow-#245570 Style Renderer
===================================================================
Implements the exact visual and structural specification from:
- Theme: Rainbow-#245570-MULTI_LINE_COLORS
- Central Topic Fill: #245570 (Text: #FFFFFF, 30px, Center-aligned)
- Main Topics: #0B5D99, #534E96, #7B4083, #A23E6A, #C34150, #CA5835 (24px, Center-aligned)
- Subtopics: Rounded Rectangle boxes (`roundedRect`), 20px, Right-aligned
- Branch Connection: Orthogonal 90-degree Elbow steps (`org.xmind.branchConnection.elbow`)
- Native `.xmind` archive generator (`content.json`, `manifest.json`, `metadata.json`)
"""

from __future__ import annotations

import asyncio
import base64
import functools
import html
import io
import json
import logging
import re
import uuid
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from parser import Node

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_FONTS_DIR = BASE_DIR / "fonts"

PAGE_TIMEOUT_MS = 25_000

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

# سایز فونت‌ها: تاپیک مرکزی ۳۰، شاخه‌های اصلی ۲۴، زیرشاخه‌ها ۲۰
FONT_SIZE_CENTRAL = 30
FONT_SIZE_MAIN = 24
FONT_SIZE_SUB = 20

# پالت رنگی دقیق استخراج‌شده از Xmind (Rainbow-#245570-MULTI_LINE_COLORS)
RAINBOW_PALETTE: List[str] = [
    "#0B5D99",  # شاخه ۱: آبی کلاسیک
    "#534E96",  # شاخه ۲: نیلی / بنفش تیره
    "#7B4083",  # شاخه ۳: ارغوانی / بنفش یاسی
    "#A23E6A",  # شاخه ۴: شرابی / بورگاندی
    "#C34150",  # شاخه ۵: یاقوتی / قرمز تیره
    "#CA5835",  # شاخه ۶: نارنجی گرم / کهربایی
]

ROOT_BG = "#245570"       # سرمه‌ای نفتی رسمی Xmind
ROOT_COLOR = "#FFFFFF"    # سفید خالص
SUBTEXT_COLOR = "#1E293B" # سرمه‌ای تیره برای خوانایی کامل زیرشاخه‌ها
CANVAS_BG = "#FFFFFF"     # پس‌زمینه سفید خالص مطابق Map Fill

PAD_X = 95
PAD_Y = 85
BRANCH_GAP = 68
SIBLING_GAP = 22
CHILD_GAP = 12
SUBTREE_GAP = 48


@dataclass(frozen=True)
class RenderResult:
    png_bytes: bytes
    pdf_bytes: bytes
    width: int
    height: int
    font_key: str
    font_label: str
    xmind_bytes: bytes = b""


# ---------------------------------------------------------------------------
# BiDi & Emoji Formatter (Fixes Parentheses Mirroring & Emoji Overlap)
# ---------------------------------------------------------------------------

_BIDI_TOKEN_RE = re.compile(
    r"\([A-Za-z0-9_./+:=#&*,\-–—\%\s]+\)"
    r"|<[A-Za-z0-9_.:/\-]+>"
    r"|[A-Za-z0-9][A-Za-z0-9_./+:=#&*\-]*(?:\s+[A-Za-z0-9_./+:=#&*\-]+)*"
)

_EMOJI_RE = re.compile(
    "["
    "\U0001F300-\U0001FAFF"
    "\U00002600-\U000027BF"
    "\U0000FE00-\U0000FE0F"
    "\U0001F1E6-\U0001F1FF"
    "]+",
    flags=re.UNICODE,
)


def _isolate_emojis_in_escaped_text(escaped_text: str) -> str:
    """جداسازی اموجی‌ها با فاصله ایمن تا روی حروف فارسی (مثل «🎯 مدیریت») نیفتند."""
    return _EMOJI_RE.sub(
        lambda m: f'<span class="emoji-token">{m.group(0)}</span>',
        escaped_text,
    )


def format_bidi_html(text: str) -> str:
    """ایزولاسیون عبارات انگلیسی، پرانتزها و اموجی‌ها در متن فارسی."""
    parts: List[str] = []
    last_idx = 0

    for match in _BIDI_TOKEN_RE.finditer(text):
        start, end = match.span()
        token = match.group(0)

        if not re.search(r"[A-Za-z()<>]", token):
            continue

        if start > last_idx:
            seg = html.escape(text[last_idx:start])
            parts.append(_isolate_emojis_in_escaped_text(seg))

        parts.append(
            f'<bdi class="ltr-token" dir="ltr">&lrm;{html.escape(token)}&lrm;</bdi>'
        )
        last_idx = end

    if last_idx < len(text):
        seg = html.escape(text[last_idx:])
        parts.append(_isolate_emojis_in_escaped_text(seg))

    return "".join(parts)


# ---------------------------------------------------------------------------
# Color Helper for Subtopic Tinted Boxes
# ---------------------------------------------------------------------------


def _hex_to_rgba(hex_color: str, alpha: float) -> str:
    h = hex_color.lstrip("#")
    if len(h) == 6:
        r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
        return f"rgba({r},{g},{b},{alpha:.2f})"
    return f"rgba(36,85,112,{alpha:.2f})"


# ---------------------------------------------------------------------------
# Smart Font Resolver & Base64 Data URI Injector
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Balanced Clockwise Tree Split
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# HTML, CSS & Xmind Elbow SVG Builders
# ---------------------------------------------------------------------------


def _node_style(level: int, color: str) -> str:
    """
    استایل کادرها دقیقاً منطبق با `xmind_template.json` (org.xmind.topicShape.roundedRect):
    - سطح ۰ (تاپیک مرکزی): پس‌زمینه #245570، متن سفید، سایز ۳۰، وسط‌چین، بدون کادر اضافه.
    - سطح ۱ (شاخه‌های اصلی): پس‌زمینه پالت ۶ رنگ، متن سفید، سایز ۲۴، وسط‌چین.
    - سطح ۲ به بالا (زیرشاخه‌ها): کادر مستطیلی با گوشه‌های گرد و تینت ملایم هم‌رنگ شاخه، سایز ۲۰، راست‌چین.
    """
    if level == 0:
        return (
            f"background:{ROOT_BG};border:none;"
            f"color:{ROOT_COLOR};border-radius:14px;"
            "padding:18px 36px;"
            f"font-size:{FONT_SIZE_CENTRAL}px;font-weight:700;"
            "text-align:center;justify-content:center;"
            "box-shadow:0 4px 14px rgba(36,85,112,0.22);"
        )
    if level == 1:
        return (
            f"background:{color};border:none;color:#FFFFFF;"
            "border-radius:11px;padding:12px 26px;"
            f"font-size:{FONT_SIZE_MAIN}px;font-weight:600;"
            "text-align:center;justify-content:center;"
            "box-shadow:0 2px 8px rgba(0,0,0,0.10);"
        )
    # سطح ۲+ (Subtopic roundedRect در تم Rainbow-#245570)
    tint_bg = _hex_to_rgba(color, 0.11 if level == 2 else 0.07)
    tint_border = _hex_to_rgba(color, 0.28 if level == 2 else 0.20)
    return (
        f"background:{tint_bg};"
        f"border:1.5px solid {tint_border};"
        f"color:{SUBTEXT_COLOR};"
        "border-radius:9px;"
        "padding:7px 16px;"
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


# موتور ترسیم خطوط پله‌ای / قائمه ۹۰ درجه (org.xmind.branchConnection.elbow)
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

  /*
   * ترسیم خط پله‌ای قائمه (Xmind Elbow: org.xmind.branchConnection.elbow)
   * با گوشه‌های ۹۰ درجه استاندارد و انحنای ظریف در زاویه شکست
   */
  function makeElbowPath(x1, y1, x2, y2, side, cornerRadius) {
    const dy = y2 - y1;
    const ady = Math.abs(dy);
    if (ady < 1.5) {
      return `M ${x1},${y1} L ${x2},${y2}`;
    }

    const dirX = (side === 'right') ? 1 : -1;
    const dirY = (dy >= 0) ? 1 : -1;
    const midX = x1 + (x2 - x1) * 0.45;
    const r = Math.min(cornerRadius, ady / 2, Math.abs(midX - x1) * 0.8, Math.abs(x2 - midX) * 0.8);

    if (r < 1.5) {
      return `M ${x1},${y1} L ${midX},${y1} L ${midX},${y2} L ${x2},${y2}`;
    }

    return (
      `M ${x1},${y1} ` +
      `L ${midX - dirX * r},${y1} ` +
      `Q ${midX},${y1} ${midX},${y1 + dirY * r} ` +
      `L ${midX},${y2 - dirY * r} ` +
      `Q ${midX},${y2} ${midX + dirX * r},${y2} ` +
      `L ${x2},${y2}`
    );
  }

  function drawElbowConnection(parentEl, childEl, side, connectionLevel, color) {
    const pr = toLocal(parentEl.getBoundingClientRect());
    const cr = toLocal(childEl.getBoundingClientRect());

    const pathEl = document.createElementNS('http://www.w3.org/2000/svg', 'path');

    const x1 = (side === 'right') ? pr.right : pr.x;
    const y1 = pr.cy;
    const x2 = (side === 'right') ? cr.x : cr.right;
    const y2 = cr.cy;

    if (connectionLevel === 0) {
      /* اتصال تاپیک مرکزی به شاخه‌های اصلی: ضخامت 3pt (معادل 3.5px) و بدون Tapered */
      pathEl.setAttribute('d', makeElbowPath(x1, y1, x2, y2, side, 8));
      pathEl.setAttribute('fill', 'none');
      pathEl.setAttribute('stroke', color);
      pathEl.setAttribute('stroke-width', '3.5');
      pathEl.setAttribute('stroke-linecap', 'round');
      pathEl.setAttribute('stroke-linejoin', 'round');
    } else {
      /* اتصال شاخه‌های اصلی به زیرشاخه‌ها: ضخامت 2pt (معادل 2.4px) پله‌ای */
      pathEl.setAttribute('d', makeElbowPath(x1, y1, x2, y2, side, 6));
      pathEl.setAttribute('fill', 'none');
      pathEl.setAttribute('stroke', color);
      pathEl.setAttribute('stroke-width', '2.4');
      pathEl.setAttribute('stroke-linecap', 'round');
      pathEl.setAttribute('stroke-linejoin', 'round');
    }

    svg.appendChild(pathEl);
  }

  function traverse(bgEl, parentNodeEl, side, connectionLevel) {
    const nodeEl = bgEl.querySelector(':scope > .node');
    const color  = bgEl.dataset.color;
    if (parentNodeEl && nodeEl) {
      drawElbowConnection(parentNodeEl, nodeEl, side, connectionLevel, color);
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
  font-family: '{font_family}', 'NeverMind', 'Tahoma', 'Noto Color Emoji', 'Segoe UI Emoji', 'Apple Color Emoji', sans-serif;
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

.node.level-0    {{ max-width: 420px; min-width: 190px; }}
.node.level-1    {{ max-width: 330px; min-width: 140px; }}
.node.level-2    {{ max-width: 390px; min-width: 90px;  }}
.node.level-deep {{ max-width: 460px; min-width: 90px;  }}

.node-inner {{
  direction: rtl;
  unicode-bidi: isolate;
  display: block;
  width: 100%;
}}

/* وسط‌چین شدن تاپیک مرکزی (۳۰) و شاخه‌های اصلی (۲۴) */
.node.level-0 .node-inner,
.node.level-1 .node-inner {{
  text-align: center;
}}

/* راست‌چین شدن زیرشاخه‌ها (۲۰) در هر دو سمت چپ و راست */
.node.level-2 .node-inner,
.node.level-deep .node-inner {{
  text-align: right;
}}

.ltr-token {{
  direction: ltr;
  unicode-bidi: isolate;
  display: inline;
}}

.emoji-token {{
  display: inline-block;
  margin: 0 4px;
  font-family: 'Noto Color Emoji', 'Segoe UI Emoji', 'Apple Color Emoji', sans-serif;
  vertical-align: middle;
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
        root_style=_node_style(0, ROOT_BG),
        root_text=format_bidi_html(root.text),
        right_html=right_html,
        left_tree_html=left_tree_html,
        js_draw=_JS_DRAW,
    )


# ---------------------------------------------------------------------------
# Native `.xmind` File Builder (Matches `xmind_template.json` Schema)
# ---------------------------------------------------------------------------


def _node_to_xmind_topic(node: Node, branch_color: Optional[str] = None) -> Dict[str, Any]:
    topic_id = f"topic-{uuid.uuid4().hex[:12]}"
    if node.level == 1 and branch_color:
        topic_data: Dict[str, Any] = {
            "id": topic_id,
            "title": node.text,
            "style": {
                "id": f"style-{uuid.uuid4().hex[:8]}",
                "properties": {
                    "svg:fill": branch_color,
                    "line-color": branch_color,
                    "fo:color": "#FFFFFF",
                    "line-class": "org.xmind.branchConnection.elbow",
                    "shape-class": "org.xmind.topicShape.roundedRect",
                },
            },
        }
    else:
        topic_data = {
            "id": topic_id,
            "title": node.text,
            "style": {
                "properties": {
                    "line-class": "org.xmind.branchConnection.elbow",
                    "shape-class": "org.xmind.topicShape.roundedRect",
                }
            },
        }

    if node.children:
        topic_data["children"] = {
            "attached": [
                _node_to_xmind_topic(c, branch_color=branch_color)
                for c in node.children
            ]
        }
    return topic_data


def build_xmind_bytes(root: Node) -> bytes:
    """تولید مستقیم فایل `.xmind` در حافظه رم مطابق با ساختار `xmind_template.json`."""
    root.normalize_levels(0)
    sheet_id = "sheet-root-01"

    attached_main_topics = [
        _node_to_xmind_topic(
            child,
            branch_color=RAINBOW_PALETTE[idx % len(RAINBOW_PALETTE)],
        )
        for idx, child in enumerate(root.children)
    ]

    content_json_data = [
        {
            "id": sheet_id,
            "class": "sheet",
            "title": root.text[:50] or "Map 1",
            "rootTopic": {
                "id": "central-topic-id",
                "class": "topic",
                "title": root.text,
                "structureClass": "org.xmind.ui.map.clockwise",
                "children": {"attached": attached_main_topics},
            },
            "extensions": [
                {
                    "provider": "org.xmind.ui.skeleton.structure.style",
                    "content": {
                        "centralTopic": "org.xmind.ui.map.clockwise",
                        "mainTopic": "org.xmind.ui.logic.right",
                    },
                }
            ],
            "theme": {
                "id": "theme-rainbow-elbow",
                "colorThemeId": "Rainbow-#245570-MULTI_LINE_COLORS",
                "skeletonThemeId": "db4a5df4db39a8cd1310ea55ea",
                "map": {
                    "id": "map-style-id",
                    "properties": {
                        "svg:fill": "#ffffff",
                        "multi-line-colors": "#0B5D99 #534E96 #7B4083 #A23E6A #C34150 #CA5835",
                        "color-list": "#000229 #1F2766 #52CC83 #4D86DB #99142F #245570",
                        "line-tapered": "none",
                    },
                },
                "centralTopic": {
                    "id": "ct-style-id",
                    "properties": {
                        "svg:fill": "#245570",
                        "line-color": "#245570",
                        "fo:color": "#FFFFFF",
                        "shape-class": "org.xmind.topicShape.roundedRect",
                        "line-class": "org.xmind.branchConnection.elbow",
                        "line-width": "3pt",
                        "line-pattern": "solid",
                        "fill-pattern": "solid",
                        "border-line-width": "0pt",
                        "fo:font-family": "NeverMind",
                        "fo:font-size": "30pt",
                        "fo:font-weight": 500,
                        "fo:text-align": "center",
                    },
                },
                "mainTopic": {
                    "id": "mt-style-id",
                    "properties": {
                        "shape-class": "org.xmind.topicShape.roundedRect",
                        "line-class": "org.xmind.branchConnection.elbow",
                        "line-width": "2pt",
                        "fill-pattern": "solid",
                        "border-line-width": "0pt",
                        "fo:font-family": "NeverMind",
                        "fo:font-size": "18pt",
                        "fo:font-weight": 500,
                        "fo:text-align": "center",
                    },
                },
                "subTopic": {
                    "id": "st-style-id",
                    "properties": {
                        "shape-class": "org.xmind.topicShape.roundedRect",
                        "line-class": "org.xmind.branchConnection.elbow",
                        "line-width": "2pt",
                        "fill-pattern": "solid",
                        "border-line-width": "0pt",
                        "fo:font-family": "NeverMind",
                        "fo:font-size": "14pt",
                        "fo:font-weight": 400,
                        "fo:text-align": "right",
                    },
                },
            },
            "topicPositioning": "fixed",
            "coreVersion": "2.99.0",
        }
    ]

    manifest = {"file-entries": {"content.json": {}, "metadata.json": {}}}
    metadata = {
        "modifier": "Vana 22.11.3656",
        "dataStructureVersion": "1",
        "layoutEngineVersion": "2",
        "activeSheetId": sheet_id,
    }

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("content.json", json.dumps(content_json_data, ensure_ascii=False))
        zf.writestr("manifest.json", json.dumps(manifest))
        zf.writestr("metadata.json", json.dumps(metadata))
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Singleton Playwright Browser Manager
# ---------------------------------------------------------------------------


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
    normalized_key = font_key if font_key in FONT_CONFIG else "font_a"
    font_label = str(FONT_CONFIG[normalized_key]["button_label"])

    html_content = build_mindmap_html(
        root=root, font_key=normalized_key, fonts_dir=fonts_dir
    )
    xmind_bytes = build_xmind_bytes(root)

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

        try:
            await asyncio.wait_for(
                page.evaluate("() => document.fonts ? document.fonts.ready : Promise.resolve()"),
                timeout=8.0,
            )
        except Exception:
            logger.warning("Font ready wait timed out; continuing render.")

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

        locator = page.locator("#page-root")
        png_bytes = await locator.screenshot(type="png", timeout=PAGE_TIMEOUT_MS)

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
            xmind_bytes=xmind_bytes,
        )
    finally:
        try:
            await ctx.close()
        except Exception:
            pass
