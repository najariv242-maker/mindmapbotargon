"""renderer.py — Async Playwright (Chromium) mind-map renderer.

Pipeline:
  1. FONT_CONFIG (top of file) declares font assets: file_path / family /
     button_label. All .ttf/.woff2 files under the project `fonts/` folder are
     discovered and embedded as Base64 data URIs inside `@font-face`, so no
     CORS or file:// restrictions can ever affect rendering.
  2. Deterministic balanced layout (pure Python): root centered, level-1
     branches greedily balanced right/left by subtree height, per-depth
     columns, vertically packed siblings, cubic-bezier SVG connectors.
  3. Persistent Chromium browser + one isolated BrowserContext per render,
     all network routes aborted, canvas limits enforced, document.fonts.ready
     awaited, full-page PNG (configurable scale) + single-page PDF produced
     in-memory (io.BytesIO), cancellation-safe cleanup in finally.

No Playwright import at module import time (lazy) — the module can be tested
without a browser installed.
"""

from __future__ import annotations

import asyncio
import base64
import html
import io
import logging
import mimetypes
from dataclasses import dataclass, field
from pathlib import Path
from typing import Sequence

from parser import Node, Limits, DEFAULT_LIMITS

logger = logging.getLogger("renderer")

# --------------------------------------------------------------------------- #
# FONT_CONFIG — edit button_label values to change Telegram inline buttons.
# --------------------------------------------------------------------------- #
BASE_DIR = Path(__file__).resolve().parent

FONT_CONFIG: list[dict] = [
    {
        "file_path": "fonts/Font_Option_1.ttf",  # e.g. Farhang2 / فرهنگ ۲
        "family": "MindFontA",
        "button_label": "فونت ۱ — فرهنگ",
    },
    {
        "file_path": "fonts/Font_Option_2.ttf",  # e.g. Vazirmatn / وزیرمتن
        "family": "MindFontB",
        "button_label": "فونت ۲ — وزیرمتن",
    },
]

# --------------------------------------------------------------------------- #
# Render configuration
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class RenderConfig:
    max_canvas_width: int = 12000
    max_canvas_height: int = 12000
    png_device_scale_factor: int = 3
    pdf_enabled: bool = True
    render_timeout_seconds: float = 45.0
    max_concurrent_renders: int = 2
    node_font_size: int = 26
    root_font_size: int = 38
    node_padding_x: int = 22
    node_padding_y: int = 14
    level_gap_x: int = 120          # horizontal gap between depth columns
    sibling_gap_y: int = 18         # vertical gap between sibling boxes
    branch_gap_y: int = 40          # extra gap between level-1 branches
    palette: tuple[str, ...] = (
        "#2563EB", "#7C3AED", "#DB2777",
        "#059669", "#D97706", "#DC2626", "#0891B2", "#65A30D",
    )

DEFAULT_RENDER_CONFIG = RenderConfig()


# --------------------------------------------------------------------------- #
# Typed results / errors
# --------------------------------------------------------------------------- #

class RenderError(Exception):
    """Raised when rendering fails (config, limits, browser, or timeout)."""


@dataclass(slots=True)
class RenderResult:
    png: bytes
    pdf: bytes | None
    width_px: int
    height_px: int
    font_label: str
    font_family: str
    node_count: int


@dataclass(slots=True)
class FontAsset:
    family: str
    button_label: str
    path: Path
    mime: str
    data_uri: str
    size_bytes: int


# --------------------------------------------------------------------------- #
# Font discovery / validation / embedding
# --------------------------------------------------------------------------- #

_MAGIC = {
    b"\x00\x01\x00\x00": "font/ttf",     # TrueType
    b"OTTO": "font/otf",                  # OpenType
    b"wOF2": "font/woff2",
    b"wOFF": "font/woff",
}


def _sniff_mime(data: bytes, fallback: str) -> str:
    for magic, mime in _MAGIC.items():
        if data[: len(magic)] == magic:
            return mime
    return fallback or (mimetypes.guess_type("f.ttf")[0] or "font/ttf")


def discover_font_files() -> list[Path]:
    """All .ttf/.otf/.woff/.woff2 files under the project fonts folder."""
    fonts_dir = BASE_DIR / "fonts"
    if not fonts_dir.is_dir():
        return []
    files: list[Path] = []
    for pattern in ("*.ttf", "*.otf", "*.woff", "*.woff2"):
        files.extend(sorted(fonts_dir.glob(pattern)))
    return files


def load_font_assets(config: Sequence[dict] | None = None) -> list[FontAsset]:
    """Validate configured fonts and embed them as Base64 data URIs."""
    assets: list[FontAsset] = []
    available = {p.name: p for p in discover_font_files()}
    for entry in (config or FONT_CONFIG):
        raw_path = Path(entry["file_path"])
        path = raw_path if raw_path.is_absolute() else BASE_DIR / raw_path
        if not path.is_file():
            raise RenderError(
                f"فونت پیکربندی‌شده یافت نشد: {entry['file_path']}"
            )
        if path.name not in available:
            logger.warning("Font %s is not inside the project fonts folder.", path.name)
        data = path.read_bytes()
        if len(data) < 100:
            raise RenderError(f"فایل فونت نامعتبر یا خراب است: {path.name}")
        mime = _sniff_mime(data, mimetypes.guess_type(path.name)[0] or "font/ttf")
        b64 = base64.b64encode(data).decode("ascii")
        assets.append(
            FontAsset(
                family=str(entry["family"]),
                button_label=str(entry["button_label"]),
                path=path,
                mime=mime,
                data_uri=f"data:{mime};base64,{b64}",
                size_bytes=len(data),
            )
        )
    if not assets:
        raise RenderError("هیچ فونتی در FONT_CONFIG پیکربندی نشده است.")
    return assets


# --------------------------------------------------------------------------- #
# Deterministic balanced layout (pure geometry)
# --------------------------------------------------------------------------- #

@dataclass(slots=True)
class _Box:
    id: int
    node: Node
    x: float = 0.0
    y: float = 0.0
    w: float = 0.0
    h: float = 0.0
    color: str = "#1E293B"
    side: int = 0          # -1 left, +1 right, 0 root
    depth: int = 0


def _estimate_size(text: str, font_px: int, cfg: RenderConfig) -> tuple[float, float]:
    """Deterministic text-size estimate (CJK/wide chars count double)."""
    units = sum(2 if ord(c) > 0x2E7F else 1 for c in text)
    w = cfg.node_padding_x * 2 + units * font_px * 0.60
    h = cfg.node_padding_y * 2 + font_px * 1.35
    return w, min(h, font_px * 2.7)


def _assign_colors(node: Node, cfg: RenderConfig) -> dict[int, str]:
    """Color inherited per level-1 branch (rainbow across branches)."""
    colors: dict[int, str] = {}
    for i, child in enumerate(node.children):
        color = cfg.palette[i % len(cfg.palette)]
        stack = [child]
        while stack:
            n = stack.pop()
            colors[id(n)] = color
            stack.extend(n.children)
    return colors


def _subtree_height(box: _Box, boxes: dict[int, _Box], cfg: RenderConfig) -> float:
    kids = [boxes[id(c)] for c in box.node.children]
    if not kids:
        return box.h
    total = sum(_subtree_height(k, boxes, cfg) for k in kids)
    total += cfg.sibling_gap_y * (len(kids) - 1)
    return max(box.h, total)


def _place(box: _Box, y_top: float, boxes: dict[int, _Box], cfg: RenderConfig) -> None:
    kids = [boxes[id(c)] for c in box.node.children]
    box.y = y_top
    if not kids:
        return
    y = y_top
    for k in kids:
        k.x = box.x + box.side * (box.w / 2 + cfg.level_gap_x + k.w / 2)
        _place(k, y, boxes, cfg)
        y += _subtree_height(k, boxes, cfg) + cfg.sibling_gap_y


def compute_layout(root: Node, cfg: RenderConfig) -> tuple[list[_Box], float, float]:
    """Deterministic balanced mind-map layout. Returns (boxes, width, height)."""
    boxes: dict[int, _Box] = {}
    counter = [0]

    def make(node: Node, depth: int, side: int, color: str) -> _Box:
        counter[0] += 1
        font = cfg.root_font_size if depth == 0 else cfg.node_font_size
        w, h = _estimate_size(node.text, font, cfg)
        b = _Box(id=counter[0], node=node, w=w, h=h, color=color,
                 side=side, depth=depth)
        boxes[id(node)] = b
        for c in node.children:
            make(c, depth + 1, side or (1 if depth == 0 else side), color)
        return b

    root_box = make(root, 0, 0, "#1E293B")
    branches = [boxes[id(c)] for c in root.children]

    # Greedy balance of level-1 branches by subtree height (right first, like XMind).
    heights = [_subtree_height(b, boxes, cfg) for b in branches]
    right_idx: list[int] = []
    left_idx: list[int] = []
    right_h = left_h = 0.0
    for i in sorted(range(len(branches)), key=lambda j: -heights[j]):
        if right_h <= left_h:
            right_idx.append(i); right_h += heights[i] + cfg.branch_gap_y
        else:
            left_idx.append(i); left_h += heights[i] + cfg.branch_gap_y
    right_idx.sort(); left_idx.sort()
    for i in right_idx:
        branches[i].side = 1
    for i in left_idx:
        branches[i].side = -1

    # Column x-offsets per depth per side (based on widest box of that depth).
    def column_offsets(side: int) -> dict[int, float]:
        widest: dict[int, float] = {}
        for b in boxes:
            if b.side == side and b.depth >= 1:
                widest[b.depth] = max(widest.get(b.depth, 0.0), b.w)
        offs: dict[int, float] = {}
        x = root_box.w / 2 + cfg.level_gap_x
        for d in sorted(widest):
            offs[d] = x + widest[d] / 2
            x += widest[d] + cfg.level_gap_x
        return offs

    off_r = column_offsets(1)
    off_l = column_offsets(-1)
    for b in boxes:
        if b.depth == 0:
            continue
        offs = off_r if b.side == 1 else off_l
        b.x = b.side * offs.get(b.depth, 0.0)

    # Vertical packing per side, root centered on the taller side.
    def pack_side(idxs: list[int], center_y: float) -> tuple[float, float]:
        if not idxs:
            return center_y, center_y
        total = sum(_subtree_height(branches[i], boxes, cfg) for i in idxs)
        total += cfg.branch_gap_y * (len(idxs) - 1)
        y = center_y - total / 2
        top, bottom = y, y + total
        for i in idxs:
            b = branches[i]
            h = _subtree_height(b, boxes, cfg)
            _place(b, y + (h - b.h) / 2, boxes, cfg)
            y += h + cfg.branch_gap_y
        return top, bottom

    center_y = max(right_h, left_h) / 2 + root_box.h
    rt, rb = pack_side(right_idx, center_y)
    lt, lb = pack_side(left_idx, center_y)

    # Normalize so everything starts at (0,0) with margins.
    margin = 60
    min_x = min(b.x - b.w / 2 for b in boxes.values())
    min_y = min(min(rt, lt, center_y - root_box.h / 2), 0)
    max_x = max(b.x + b.w / 2 for b in boxes.values())
    max_y = max(rb, lb, center_y + root_box.h / 2)
    width = max_x - min_x + margin * 2
    height = max_y - min_y + margin * 2
    for b in boxes:
        b.x -= min_x - margin
        b.y -= min_y - margin
    root_box.x = (max_x - min_x) / 2 + margin
    root_box.y = (max_y - min_y) / 2 + margin
    return list(boxes.values()), width, height


def _connector(x1: float, y1: float, x2: float, y2: float, color: str) -> str:
    mx = x1 + (x2 - x1) * 0.5
    return (
        f'<path d="M {x1:.1f} {y1:.1f} C {mx:.1f} {y1:.1f}, '
        f'{mx:.1f} {y2:.1f}, {x2:.1f} {y2:.1f}" fill="none" '
        f'stroke="{color}" stroke-width="3.2" stroke-linecap="round"/>'
    )


def build_svg(root: Node, cfg: RenderConfig) -> tuple[str, int, int]:
    """Build the final standalone SVG document (escaped text, RTL)."""
    boxes, width, height = compute_layout(root, cfg)
    by_node = {id(b.node): b for b in boxes}
    parts: list[str] = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width:.0f}" '
        f'height="{height:.0f}" viewBox="0 0 {width:.0f} {height:.0f}">',
        "<defs><style>",
        "text{direction:rtl;unicode-bidi:isolate;text-align:center;"
        "dominant-baseline:central;}",
        "</style></defs>",
        f'<rect width="100%" height="100%" fill="#F8FAFC"/>',
    ]
    # Connectors first (under boxes); walk the tree so parents are reliable.
    def walk(node: Node, parent: _Box | None) -> None:
        b = by_node[id(node)]
        if parent is not None and b.depth > 0:
            x1 = parent.x + parent.side * parent.w / 2
            y1 = parent.y
            x2 = b.x - b.side * b.w / 2
            y2 = b.y
            parts.append(_connector(x1, y1, x2, y2, b.color))
        for c in node.children:
            walk(c, b)
    walk(root, None)
    # Boxes.
    for b in boxes:
        font = cfg.root_font_size if b.depth == 0 else cfg.node_font_size
        fill = b.color if b.depth <= 1 else "#FFFFFF"
        text_fill = "#FFFFFF" if b.depth <= 1 else "#0F172A"
        stroke = "none" if b.depth <= 1 else b.color
        rx = 14 if b.depth == 0 else 10
        parts.append(
            f'<rect x="{b.x - b.w/2:.1f}" y="{b.y - b.h/2:.1f}" '
            f'width="{b.w:.1f}" height="{b.h:.1f}" rx="{rx}" fill="{fill}" '
            f'stroke="{stroke}" stroke-width="2.4"/>'
        )
        parts.append(
            f'<text x="{b.x:.1f}" y="{b.y:.1f}" font-size="{font}" '
            f'fill="{text_fill}" text-anchor="middle" '
            f'textContent="{html.escape(b.node.text)}">'
            f'{html.escape(b.node.text)}</text>'
        )
    parts.append("</svg>")
    return "".join(parts), int(width), int(height)


def build_html(svg: str, fonts: Sequence[FontAsset], family: str,
               width: int, height: int) -> str:
    """Full HTML document with Base64 @font-face injection."""
    faces = "".join(
        f"@font-face{{font-family:'{f.family}';"
        f"src:url({f.data_uri}) format('{('woff2' if f.mime=='font/woff2' else 'truetype')}');"
        f"font-weight:normal;font-style:normal;}}"
        for f in fonts
    )
    return (
        "<!DOCTYPE html><html lang=\"fa\" dir=\"rtl\"><head><meta charset=\"utf-8\">"
        f"<style>{faces}"
        "html,body{margin:0;padding:0;background:#F8FAFC;}"
        f"body{{width:{width}px;height:{height}px;}}"
        "svg text{font-family:'" + family + "','Vazirmatn','Segoe UI',sans-serif;}"
        "</style></head><body>" + svg + "</body></html>"
    )


# --------------------------------------------------------------------------- #
# Async Chromium renderer (persistent browser, isolated contexts)
# --------------------------------------------------------------------------- #

class Renderer:
    """Persistent-browser renderer with per-render isolated contexts."""

    def __init__(self, config: RenderConfig = DEFAULT_RENDER_CONFIG) -> None:
        self.config = config
        self._semaphore = asyncio.Semaphore(config.max_concurrent_renders)
        self._browser = None            # playwright Browser (lazy)
        self._playwright = None
        self._fonts: list[FontAsset] | None = None
        self._lock = asyncio.Lock()

    # -- lifecycle ---------------------------------------------------------- #

    async def start(self) -> None:
        """Launch the persistent headless Chromium (lazy Playwright import)."""
        async with self._lock:
            if self._browser is not None:
                return
            from playwright.async_api import async_playwright  # lazy import
            self._playwright = await async_playwright().start()
            self._browser = await self._playwright.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-dev-shm-usage",
                      "--disable-gpu", "--no-first-run"],
            )
            logger.info("Persistent Chromium started.")

    async def stop(self) -> None:
        async with self._lock:
            for closer in (self._browser, self._playwright):
                if closer is not None:
                    try:
                        await closer.close()
                    except Exception:
                        logger.warning("Error during renderer shutdown.", exc_info=True)
            self._browser = None
            self._playwright = None
            logger.info("Chromium stopped.")

    # -- fonts ---------------------------------------------------------------- #

    def fonts(self) -> list[FontAsset]:
        if self._fonts is None:
            self._fonts = load_font_assets()
        return self._fonts

    # -- rendering ------------------------------------------------------------ #

    async def render(self, root: Node, font_index: int = 0,
                     config: RenderConfig | None = None) -> RenderResult:
        """Render tree -> (PNG bytes, optional PDF bytes). Never leaks the browser."""
        cfg = config or self.config
        fonts = self.fonts()
        if not 0 <= font_index < len(fonts):
            raise RenderError("شماره فونت انتخاب‌شده نامعتبر است.")
        font = fonts[font_index]
        limits = Limits()

        node_count = root.count_nodes()
        if node_count > limits.max_nodes:
            raise RenderError("تعداد گره‌ها برای رندر بیش از حد مجاز است.")
        if root.max_depth() > limits.max_depth:
            raise RenderError("عمق نقشه برای رندر بیش از حد مجاز است.")

        if self._browser is None:
            await self.start()

        svg, width, height = build_svg(root, cfg)
        if width > cfg.max_canvas_width or height > cfg.max_canvas_height:
            raise RenderError(
                f"ابعاد نقشه ({width:.0f}×{height:.0f}) از حد مجاز بوم "
                f"({cfg.max_canvas_width}×{cfg.max_canvas_height}) بزرگ‌تر است."
            )
        doc = build_html(svg, fonts, font.family, width, height)

        page = None
        context = None
        async with self._semaphore:
            try:
                context = await self._browser.new_context(
                    viewport={"width": width, "height": min(height, 8000)},
                    device_scale_factor=cfg.png_device_scale_factor,
                )
                # Block ALL network (defense in depth; doc uses only data: URIs).
                await context.route("**/*", lambda route: asyncio.ensure_future(route.abort()))
                page = await context.new_page()
                await page.set_content(doc, wait_until="load",
                                       timeout=cfg.render_timeout_seconds * 1000)
                await page.evaluate("document.fonts.ready.then(() => true)")
                png_bytes = await page.screenshot(full_page=True, type="png")
                pdf_bytes: bytes | None = None
                if cfg.pdf_enabled:
                    pdf_bytes = await page.pdf(
                        width=f"{width}px", height=f"{height}px",
                        print_background=True,
                        margin={"top": "0", "bottom": "0", "left": "0", "right": "0"},
                    )
                logger.info(
                    "Rendered %d nodes as %dx%d CSS px (font=%s).",
                    node_count, width, height, font.family,
                )
                return RenderResult(
                    png=png_bytes, pdf=pdf_bytes, width_px=width,
                    height_px=height, font_label=font.button_label,
                    font_family=font.family, node_count=node_count,
                )
            except asyncio.CancelledError:
                logger.info("Render cancelled; cleaning up page/context.")
                raise
            except Exception as exc:
                logger.error("Render failed: %s", type(exc).__name__)
                raise RenderError("خطا در تولید خروجی نقشه ذهنی.") from exc
            finally:
                if page is not None:
                    try:
                        await page.close()
                    except Exception:
                        pass
                if context is not None:
                    try:
                        await context.close()
                    except Exception:
                        pass


# Convenience synchronous-async entry for CLI/testing without Telegram.
async def render_tree(root: Node, font_index: int = 0,
                      config: RenderConfig = DEFAULT_RENDER_CONFIG) -> RenderResult:
    r = Renderer(config)
    try:
        return await r.render(root, font_index)
    finally:
        await r.stop()
