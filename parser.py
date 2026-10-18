"""
parser.py - Production-Ready Unified OPML & Markdown to Mind Map Tree Converter
===============================================================================
Combines the best capabilities of all 3 versions:
- Supports `.opml` XML files, `.md` Markdown files, and raw chat text.
- Ignores fenced code blocks (```...```) and horizontal rules so code comments
  (`# comment`) never corrupt heading hierarchies.
- Strips inline Markdown formatting (**bold**, `code`, [link](url)) while
  strictly preserving Persian ZWNJ (\u200c / نیم‌فاصله).
- Supports Persian digits (۰-۹), standard bullets (-, *, +, •), and plain text.
- Uses asymptotic fractional bullet depth scaling so deeply nested bullets
  never collide with subsequent Markdown headings at any nesting depth.
"""

from __future__ import annotations

import logging
import os
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data Model
# ---------------------------------------------------------------------------


@dataclass
class Node:
    """Unified tree node representing a topic in the Mind Map."""

    text: str
    level: int = 0
    children: List["Node"] = field(default_factory=list)

    def add_child(self, child: "Node") -> "Node":
        """Append a child node and normalize its level relative to this node."""
        child.level = self.level + 1
        self.children.append(child)
        return child

    def normalize_levels(self, current_level: int = 0) -> "Node":
        """Recursively assign strict 0-based depth levels across the entire tree."""
        self.level = current_level
        for child in self.children:
            child.normalize_levels(current_level + 1)
        return self

    def count_nodes(self) -> int:
        """Return total number of nodes in this subtree (including self)."""
        return 1 + sum(child.count_nodes() for child in self.children)

    def max_depth(self) -> int:
        """Return maximum depth of the subtree."""
        if not self.children:
            return self.level
        return max(child.max_depth() for child in self.children)

    def to_dict(self) -> Dict[str, Any]:
        """Serialize the node subtree to a JSON-compatible dictionary."""
        return {
            "text": self.text,
            "level": self.level,
            "children": [child.to_dict() for child in self.children],
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Node":
        """Reconstruct a Node subtree from a dictionary."""
        return cls(
            text=str(data.get("text", "بدون عنوان")),
            level=int(data.get("level", 0)),
            children=[cls.from_dict(c) for c in data.get("children", [])],
        )


# ---------------------------------------------------------------------------
# Text Cleaning & Normalization Helpers
# ---------------------------------------------------------------------------

_TAB_SIZE = 2

_INLINE_MD_PATTERNS: List[Tuple[re.Pattern[str], str]] = [
    (re.compile(r"\*\*(.+?)\*\*"), r"\1"),
    (re.compile(r"__(.+?)__"), r"\1"),
    (re.compile(r"`([^`]+)`"), r"\1"),
    (re.compile(r"~~(.+?)~~"), r"\1"),
    (re.compile(r"!\[([^\]]*)\]\([^)]+\)"), r"\1"),
    (re.compile(r"\[([^\]]+)\]\([^)]+\)"), r"\1"),
]

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+)$")
_BULLET_RE = re.compile(
    r"^([ \t]*)(?:[-*+•]|(?:\d+|[۰-۹]+)[.\)])\s+(.+)$"
)
_HR_RE = re.compile(r"^([-*_])\1{2,}$")


def clean_topic_text(raw_text: str) -> str:
    """
    Strip inline Markdown markers while preserving Persian/English/numeric tokens
    and Persian ZWNJ (\u200c).
    """
    text = raw_text.strip()
    for pattern, replacement in _INLINE_MD_PATTERNS:
        text = pattern.sub(replacement, text)
    # Collapse excessive spaces/tabs while keeping single spaces and ZWNJ (\u200c)
    text = re.sub(r"[ \t]+", " ", text)
    return text.strip()


def _stem(filename: Optional[str], default: str = "موضوع اصلی") -> str:
    """Extract clean root title from filename."""
    if not filename:
        return default
    base = os.path.splitext(os.path.basename(filename))[0]
    cleaned = clean_topic_text(base.replace("_", " ").replace("-", " "))
    return cleaned or default


def _decode_source(source: Union[str, bytes, Path]) -> str:
    """Safely decode str, bytes, or Path content while stripping UTF-8 BOM."""
    if isinstance(source, Path):
        return source.read_text(encoding="utf-8-sig", errors="replace")
    if isinstance(source, bytes):
        return source.decode("utf-8-sig", errors="replace")
    return str(source).lstrip("\ufeff")


# ---------------------------------------------------------------------------
# 1. OPML Parser (xml.etree.ElementTree)
# ---------------------------------------------------------------------------


def parse_opml(
    source: Union[str, bytes, Path],
    filename: Optional[str] = None,
    default_root_title: str = "موضوع اصلی",
) -> Node:
    """
    Parse an OPML XML string, bytes, or file path into a unified `Node` tree.
    Handles namespaces, case variations, and `<head><title>` extraction.
    """
    xml_text = _decode_source(source).strip()
    if not xml_text:
        raise ValueError("فایل OPML خالی است.")

    try:
        root_elem = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        raise ValueError(f"ساختار فایل OPML/XML نامعتبر است: {exc}") from exc

    body_elem = root_elem.find("body")
    if body_elem is None:
        for child in root_elem:
            if child.tag.lower().endswith("body"):
                body_elem = child
                break
    if body_elem is None:
        body_elem = root_elem

    def _outline_to_node(elem: ET.Element, level: int) -> Node:
        label = (
            elem.attrib.get("text")
            or elem.attrib.get("title")
            or elem.attrib.get("name")
            or (elem.text.strip() if elem.text else "")
        )
        cleaned_label = clean_topic_text(label) if label else "بدون عنوان"
        node = Node(text=cleaned_label or "بدون عنوان", level=level)
        for child_elem in elem:
            if child_elem.tag.lower().endswith("outline"):
                node.children.append(_outline_to_node(child_elem, level + 1))
        return node

    top_outlines = [
        el for el in body_elem if el.tag.lower().endswith("outline")
    ]
    if not top_outlines:
        raise ValueError("هیچ گره <outline> در فایل OPML یافت نشد.")

    if len(top_outlines) == 1:
        root_node = _outline_to_node(top_outlines[0], level=0)
        return root_node.normalize_levels(0)

    # Multiple top-level outlines: check <head><title> first, then filename stem
    head_title: Optional[str] = None
    for child in root_elem:
        if child.tag.lower().endswith("head"):
            for sub in child:
                if sub.tag.lower().endswith("title") and sub.text and sub.text.strip():
                    head_title = clean_topic_text(sub.text)
                    break

    fallback_title = head_title or _stem(filename, default=default_root_title)
    root_node = Node(text=fallback_title, level=0)
    for outline_el in top_outlines:
        root_node.add_child(_outline_to_node(outline_el, level=1))

    return root_node.normalize_levels(0)


# ---------------------------------------------------------------------------
# 2. Markdown & Structured Text Parser
# ---------------------------------------------------------------------------


def parse_markdown(
    source: Union[str, bytes, Path],
    filename: Optional[str] = None,
    default_root_title: str = "موضوع اصلی",
) -> Node:
    """
    Parse Markdown headings (`#`..`######`), nested bullet/numbered lists,
    and structured plain text into a unified `Node` tree.

    Guarantees:
    - Fenced code blocks (```) and horizontal rules (---) are ignored.
    - Bullet depth under any heading H is mapped via an asymptotic function
      `H + 0.1 + 0.8 * (indent_steps / (indent_steps + 4.0))` so bullets
      strictly remain in `[H + 0.1, H + 0.9)` and NEVER collide with deeper
      headings (`H + 1.0`) regardless of indentation depth.
    - Multiple `# H1` headings are automatically wrapped under a single root.
    """
    raw_text = _decode_source(source)
    lines = raw_text.replace("\r\n", "\n").replace("\r", "\n").split("\n")

    root_node: Optional[Node] = None
    # Stack entries: (node, kind, depth) where kind in {"h", "b"}
    stack: List[Tuple[Node, str, float]] = []
    in_code_block = False

    def _make_root(title: str) -> Node:
        nonlocal root_node
        root_node = Node(text=title, level=0)
        stack.clear()
        stack.append((root_node, "h", 0.0))
        return root_node

    def _nearest_heading_depth() -> float:
        for _, kind, depth in reversed(stack):
            if kind == "h":
                return depth
        return 0.0

    def _find_parent_for_depth(target: float) -> Node:
        while len(stack) > 1 and stack[-1][2] >= target:
            stack.pop()
        return stack[-1][0]

    def _compute_bullet_depth(heading_base: float, indent_cols: int) -> float:
        indent_steps = max(0.0, indent_cols / float(_TAB_SIZE))
        # Asymptotically approaches heading_base + 0.9 without ever reaching it
        fractional = 0.1 + 0.8 * (indent_steps / (indent_steps + 4.0))
        return heading_base + fractional

    for raw_line in lines:
        stripped = raw_line.strip()
        if not stripped:
            continue

        # Toggle fenced code blocks (ignore code comments like `# comment`)
        if stripped.startswith("```"):
            in_code_block = not in_code_block
            continue
        if in_code_block:
            continue

        # Ignore Markdown horizontal rules
        if _HR_RE.match(stripped):
            continue

        # 1. Check Heading (`#` .. `######`)
        hm = _HEADING_RE.match(stripped)
        if hm:
            hashes, content = hm.groups()
            cleaned = clean_topic_text(content)
            if not cleaned:
                continue
            depth = float(len(hashes))  # 1.0 .. 6.0
            node = Node(text=cleaned, level=int(depth) - 1)

            if depth == 1.0:
                if root_node is None:
                    root_node = node
                    root_node.level = 0
                    stack.clear()
                    stack.append((root_node, "h", 1.0))
                else:
                    # Wrap multiple H1 headings under a unified Central Topic
                    if len(stack) >= 1 and stack[0][1] == "h" and stack[0][2] == 1.0:
                        wrapper_title = _stem(filename, default=default_root_title)
                        wrapper = Node(text=wrapper_title, level=0)
                        old_root = stack[0][0]
                        wrapper.add_child(old_root)
                        root_node = wrapper
                        stack[0] = (root_node, "h", 0.0)
                    node.level = 1
                    root_node.add_child(node)
                    while len(stack) > 1:
                        stack.pop()
                    stack.append((node, "h", 1.0))
            else:
                if root_node is None:
                    _make_root(_stem(filename, default=default_root_title))
                parent = _find_parent_for_depth(depth)
                parent.add_child(node)
                stack.append((node, "h", depth))
            continue

        # 2. Check Bullet or Numbered List (supports Persian & ASCII digits)
        bm = _BULLET_RE.match(raw_line)
        if bm:
            indent_str, content = bm.groups()
            cleaned = clean_topic_text(content)
            if not cleaned:
                continue
            indent_cols = len(indent_str.expandtabs(_TAB_SIZE))

            if root_node is None:
                _make_root(_stem(filename, default=default_root_title))

            heading_base = _nearest_heading_depth()
            depth = _compute_bullet_depth(heading_base, indent_cols)

            node = Node(text=cleaned, level=0)
            parent = _find_parent_for_depth(depth)
            parent.add_child(node)
            stack.append((node, "b", depth))
            continue

        # 3. Plain Non-Empty Line Fallback (unbulleted structured text)
        cleaned = clean_topic_text(stripped)
        if not cleaned:
            continue

        leading_ws_len = len(raw_line) - len(raw_line.lstrip(" \t"))
        indent_cols = len(raw_line[:leading_ws_len].expandtabs(_TAB_SIZE))

        if root_node is None and indent_cols == 0:
            root_node = Node(text=cleaned, level=0)
            stack.clear()
            stack.append((root_node, "h", 1.0))
        else:
            if root_node is None:
                _make_root(_stem(filename, default=default_root_title))
            heading_base = _nearest_heading_depth()
            depth = _compute_bullet_depth(heading_base, indent_cols)
            node = Node(text=cleaned, level=0)
            parent = _find_parent_for_depth(depth)
            parent.add_child(node)
            stack.append((node, "b", depth))

    if root_node is None:
        raise ValueError("متن ورودی خالی است یا هیچ ساختار قابل‌پردازشی ندارد.")

    # Promote single child if synthetic root wrapper only has 1 child
    if (
        len(root_node.children) == 1
        and root_node.text == _stem(filename, default=default_root_title)
        and stack
        and stack[0][2] == 0.0
    ):
        root_node = root_node.children[0]

    return root_node.normalize_levels(0)


# ---------------------------------------------------------------------------
# 3. Unified Auto-Detecting Dispatcher
# ---------------------------------------------------------------------------


def parse_input(
    content: Union[str, bytes, Path],
    filename: Optional[str] = None,
    default_root_title: str = "موضوع اصلی",
) -> Node:
    """
    Auto-detect and parse `.opml`, `.md`, or raw Markdown/plain text into a `Node` tree.
    """
    is_opml = False
    if filename and filename.lower().endswith((".opml", ".xml")):
        is_opml = True
    elif isinstance(content, Path) and content.suffix.lower() in (".opml", ".xml"):
        is_opml = True
    else:
        sample = _decode_source(content)[:600].lstrip()
        if (
            sample.startswith("<?xml")
            or "<opml" in sample.lower()
            or "<outline" in sample.lower()
        ):
            is_opml = True

    if is_opml:
        return parse_opml(
            content, filename=filename, default_root_title=default_root_title
        )
    return parse_markdown(
        content, filename=filename, default_root_title=default_root_title
    )