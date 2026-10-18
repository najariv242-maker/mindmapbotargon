"""parser.py — Safe hierarchical parser for Markdown headings/bullets and OPML.

Parses raw UTF-8 text (Markdown headings `#`, unordered/ordered bullets) and
OPML/XML into a unified `Node(text, level, children)` tree.

Security / robustness:
- Bounded: max input bytes, max nodes, max depth, max text length.
- OPML: rejects DOCTYPE / ENTITY (XXE vector) and malformed XML before parsing.
- Bidi: rejects dangerous override controls (LRO/RLO/LRE/RLE/PDF) while
  preserving Persian ZWNJ (U+200C), ZWJ (U+200D) and safe isolates
  (LRI/RLI/FSI/PDI).
- No network, no filesystem access: pure string/bytes -> tree.

Observed in the three source implementations: V1 bounded sizes but stripped
unsafe bidi silently; V2 had no size/depth guards and accepted `Path` input;
V3 fixed bullet-depth collisions but lacked byte-level input limits. This
module keeps the strongest parts of all three.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field

__all__ = [
    "Node", "ParseError", "LimitExceededError",
    "parse_text", "parse_markdown", "parse_opml",
    "DEFAULT_LIMITS",
]

# --------------------------------------------------------------------------- #
# Limits (safe defaults; callers may override)
# --------------------------------------------------------------------------- #

@dataclass(frozen=True, slots=True)
class Limits:
    max_input_bytes: int = 200_000
    max_nodes: int = 600
    max_depth: int = 14
    max_text_length: int = 200


DEFAULT_LIMITS = Limits()

# --------------------------------------------------------------------------- #
# Errors
# --------------------------------------------------------------------------- #

class ParseError(Exception):
    """Raised when input cannot be parsed (syntax, encoding, or security)."""


class LimitExceededError(ParseError):
    """Raised when input exceeds a configured size/depth/count limit."""

# --------------------------------------------------------------------------- #
# Data model
# --------------------------------------------------------------------------- #

@dataclass(slots=True)
class Node:
    """Unified tree node: text, integer level (0 = root) and child nodes."""

    text: str
    level: int
    children: list[Node] = field(default_factory=list)

    def add_child(self, child: "Node") -> "Node":
        child.level = self.level + 1
        self.children.append(child)
        return child

    def count_nodes(self) -> int:
        return 1 + sum(c.count_nodes() for c in self.children)

    def max_depth(self) -> int:
        if not self.children:
            return 1
        return 1 + max(c.max_depth() for c in self.children)

# --------------------------------------------------------------------------- #
# Security patterns
# --------------------------------------------------------------------------- #

# Dangerous bidi override/pop controls: LRO U+202D, RLO U+202E,
# LRE U+202A, RLE U+202B, PDF U+202C.
_UNSAFE_BIDI = re.compile(r"[\u202a-\u202e]")
# Safe isolates: LRI U+2066, RLI U+2067, FSI U+2068, PDI U+2069.
_SAFE_ISOLATES = re.compile(r"[\u2066-\u2069]")
_XML_FORBIDDEN = re.compile(r"<!(?:DOCTYPE|ENTITY)", re.IGNORECASE)
_INLINE_MD = [
    (re.compile(r"\*\*(.+?)\*\*"), r"\1"),
    (re.compile(r"__(.+?)__"), r"\1"),
    (re.compile(r"`([^`]+)`"), r"\1"),
    (re.compile(r"~~(.+?)~~"), r"\1"),
    (re.compile(r"\[([^\]]+)\]\([^)]*\)"), r"\1"),
]
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")
_BULLET_RE = re.compile(r"^(\s*)([-*+\u2022])\s+(.+)$")
_ORDERED_RE = re.compile(r"^(\s*)(\d{1,3})[.)]\s+(.+)$")

def _clean_text(raw: str, max_len: int) -> str:
    """Sanitize one node title: strip unsafe bidi, inline md, control chars."""
    if _UNSAFE_BIDI.search(raw):
        raise ParseError(
            "متن شامل کاراکترهای کنترلی جهت‌دهی خطرناک (LRO/RLO/LRE/RLE/PDF) است."
        )
    text = raw.strip().lstrip("\ufeff")
    for pattern, repl in _INLINE_MD:
        text = pattern.sub(repl, text)
    out: list[str] = []
    for ch in text:
        code = ord(ch)
        if code in (0x200C, 0x200D):        # ZWNJ / ZWJ — keep
            out.append(ch)
        elif _SAFE_ISOLATES.match(ch):       # safe isolates — keep
            out.append(ch)
        elif code < 32 and ch not in "\t":
            out.append(" ")                  # normalize other controls to space
        else:
            out.append(ch)
    text = re.sub(r"[ \t]+", " ", "".join(out)).strip()
    if len(text) > max_len:
        text = text[:max_len].rstrip()
    return text

def _indent_width(s: str) -> int:
    return len(s.replace("\t", "    "))

def _make_node(raw_text: str, level: int, limits: Limits) -> Node:
    text = _clean_text(raw_text, limits.max_text_length)
    if not text:
        text = "بدون عنوان"
    return Node(text=text, level=level)

# --------------------------------------------------------------------------- #
# Markdown / plain text parsing
# --------------------------------------------------------------------------- #

def parse_text(raw: bytes | str, limits: Limits = DEFAULT_LIMITS) -> Node:
    """Parse Markdown headings and/or bullets into a rooted Node tree."""
    if isinstance(raw, bytes):
        if len(raw) > limits.max_input_bytes:
            raise LimitExceededError(
                f"حجم ورودی بیش از حد مجاز است (حداکثر {limits.max_input_bytes} بایت)."
            )
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise ParseError("ورودی باید UTF-8 معتبر باشد.") from exc
    else:
        text = raw
        if len(text.encode("utf-8")) > limits.max_input_bytes:
            raise LimitExceededError(
                f"حجم ورودی بیش از حد مجاز است (حداکثر {limits.max_input_bytes} بایت)."
            )
    text = text.replace("\r\n", "\n").replace("\r", "\n")

    # Tokenize into (kind, depth, text) items.
    items: list[tuple[str, int, str]] = []
    for line in text.split("\n"):
        if not line.strip():
            continue
        hm = _HEADING_RE.match(line.strip())
        if hm:
            items.append(("h", len(hm.group(1)), hm.group(2)))
            continue
        bm = _BULLET_RE.match(line.rstrip())
        om = _ORDERED_RE.match(line.rstrip())
        if bm:
            items.append(("b", _indent_width(bm.group(1)) // 2, bm.group(3)))
        elif om:
            items.append(("b", _indent_width(om.group(1)) // 2, om.group(3)))
        else:
            stripped = line.strip()
            # A bare line following headings/bullets becomes its own node at
            # the previous bullet depth (mixed heading/list robustness).
            items.append(("p", 0, stripped))

    if not items:
        raise ParseError("ورودی خالی است یا شامل محتوای قابل تجزیه نیست.")

    # Build tree: headings define levels; bullets nest under current heading.
    roots: list[Node] = []
    stack: list[tuple[int, str, Node]] = []   # (level, kind, node)
    node_count = 0

    bullet_stack: list[tuple[int, int]] = []  # (indent_width, effective_level)
    heading_base = -1                          # eff level of last heading seen

    def push(level: int, kind: str, raw_line: str) -> None:
        nonlocal node_count, heading_base
        node_count += 1
        if node_count > limits.max_nodes:
            raise LimitExceededError(
                f"تعداد گره‌ها بیش از حد مجاز است (حداکثر {limits.max_nodes})."
            )
        # Compute effective depth relative to stack.
        if kind == "h":
            eff = level - 1 if level > 1 else 0   # '##' -> level 1 under '#'
            heading_base = eff
            bullet_stack.clear()
        else:
            # Bullets nest by INDENT WIDTH relative to the previous bullet:
            # same indent -> sibling (flat lists stay flat); deeper indent ->
            # child of the previous bullet. A heading resets the bullet stack.
            while bullet_stack and bullet_stack[-1][0] >= level:
                bullet_stack.pop()
            if bullet_stack:
                eff = min(bullet_stack[-1][1] + 1, limits.max_depth - 1)
            elif stack:
                eff = stack[-1][0] + 1
            else:
                eff = 0
            eff = min(eff, heading_base + limits.max_depth if heading_base >= 0 else eff)
        while stack and stack[-1][0] >= eff:
            stack.pop()
        node = _make_node(raw_line, eff, limits)
        if stack:
            stack[-1][2].add_child(node)
        else:
            roots.append(node)
        stack.append((eff, kind, node))
        if kind != "h":
            bullet_stack.append((level, eff))

    for kind, depth, raw_line in items:
        push(depth, kind, raw_line)

    # Depth check across resulting forest.
    for r in roots:
        if r.max_depth() > limits.max_depth:
            raise LimitExceededError(
                f"عمق درخت بیش از حد مجاز است (حداکثر {limits.max_depth})."
            )

    if len(roots) == 1:
        return roots[0]
    # Multiple top-level items -> synthetic root (merge strategy).
    root = Node(text="نقشه ذهنی", level=0)
    for r in roots:
        root.add_child(r)
    return root

# Alias for Markdown-specific callers.
parse_markdown = parse_text

# --------------------------------------------------------------------------- #
# OPML / XML parsing
# --------------------------------------------------------------------------- #

def parse_opml(content: bytes | str, limits: Limits = DEFAULT_LIMITS,
               default_root_title: str = "نقشه ذهنی") -> Node:
    """Parse OPML/XML safely into a Node tree (rejects DOCTYPE/ENTITY)."""
    if isinstance(content, bytes):
        if len(content) > limits.max_input_bytes:
            raise LimitExceededError(
                f"حجم فایل بیش از حد مجاز است (حداکثر {limits.max_input_bytes} بایت)."
            )
        try:
            text = content.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise ParseError("فایل XML باید UTF-8 معتبر باشد.") from exc
    else:
        text = content
        if len(text.encode("utf-8")) > limits.max_input_bytes:
            raise LimitExceededError(
                f"حجم فایل بیش از حد مجاز است (حداکثر {limits.max_input_bytes} بایت)."
            )

    if _XML_FORBIDDEN.search(text):
        raise ParseError("فایل XML حاوی DOCTYPE یا ENTITY غیرمجاز است.")
    if _UNSAFE_BIDI.search(text):
        raise ParseError("فایل XML شامل کاراکترهای کنترلی جهت‌دهی خطرناک است.")

    try:
        root_el = ET.fromstring(text)
    except ET.ParseError as exc:
        raise ParseError(f"ساختار XML نامعتبر است: {exc}") from exc

    body = root_el.find("body")
    if body is None:
        for child in root_el:
            if child.tag.rsplit("}", 1)[-1].lower() == "body":
                body = child
                break
    if body is None:
        raise ParseError("المان <body> در فایل OPML یافت نشد.")

    tops = [el for el in body if el.tag.rsplit("}", 1)[-1].lower() == "outline"]
    if not tops:
        raise ParseError("هیچ گره <outline> در فایل OPML یافت نشد.")

    node_count = 0

    def opml_to_node(el: ET.Element, level: int) -> Node:
        nonlocal node_count
        node_count += 1
        if node_count > limits.max_nodes:
            raise LimitExceededError(
                f"تعداد گره‌ها بیش از حد مجاز است (حداکثر {limits.max_nodes})."
            )
        if level >= limits.max_depth:
            raise LimitExceededError(
                f"عمق درخت بیش از حد مجاز است (حداکثر {limits.max_depth})."
            )
        raw = el.get("text") or el.get("title") or el.get("name") or "بدون عنوان"
        node = _make_node(raw, level, limits)
        for child in el:
            if child.tag.rsplit("}", 1)[-1].lower() == "outline":
                node.add_child(opml_to_node(child, level + 1))
        return node

    if len(tops) == 1:
        return opml_to_node(tops[0], 0)
    root = _make_node(default_root_title, 0, limits)
    for el in tops:
        root.add_child(opml_to_node(el, 1))
    return root
