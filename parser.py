"""parser.py — converts AI JSON output to the branch-tree structure."""
from __future__ import annotations
import json
import re


def _clean_title(s: str) -> str:
    """Strip leading bullets, dashes, numbers, and whitespace."""
    return re.sub(r"^[\s\-\*\#\•\d\.]+", "", str(s)).strip()


def _normalise_node(raw: dict) -> dict:
    """Recursively normalise a branch/subtopic node."""
    title = _clean_title(raw.get("title", "بدون عنوان"))
    children = [_normalise_node(c) for c in (raw.get("children") or []) if c]
    return {"title": title, "children": children}


def parse_mindmap_json(data: dict) -> tuple[str, list[dict]]:
    """
    Expects the dict returned by OpenAI JSON mode:
    {"title": str, "branches": [...]}

    Returns (central_title, branches_list).
    """
    title = _clean_title(data.get("title", "نقشه‌ذهنی"))
    branches = [_normalise_node(b) for b in (data.get("branches") or []) if b]
    return title, branches


# Kept for backwards-compat / fallback plain-text parsing
def parse_mindmap_text(text: str) -> tuple[str, list[dict]]:
    """
    Fallback: parse indented plain text if JSON mode fails.
    First non-empty line = central title; indented lines = tree.
    """
    # Try JSON first
    try:
        data = json.loads(text)
        if isinstance(data, dict) and "branches" in data:
            return parse_mindmap_json(data)
    except (json.JSONDecodeError, TypeError):
        pass

    lines = [l for l in text.splitlines() if l.strip()]
    if not lines:
        return "نقشه‌ذهنی", []

    central = _clean_title(lines[0])
    branches: list[dict] = []
    stack: list[tuple[int, dict]] = []

    for raw in lines[1:]:
        if not raw.strip():
            continue
        indent = len(raw) - len(raw.lstrip())
        node: dict = {"title": _clean_title(raw), "children": []}

        while stack and stack[-1][0] >= indent:
            stack.pop()

        if not stack:
            branches.append(node)
        else:
            stack[-1][1]["children"].append(node)

        stack.append((indent, node))

    return central, branches
