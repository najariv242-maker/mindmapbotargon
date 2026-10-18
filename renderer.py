"""renderer.py — builds a .xmind ZIP (XMind 2023 JSON format) in memory.

Exact visual spec:
  Central topic:   fill #245570, text #FFFFFF, roundedRect, elbow, 3pt
  Main branches:   6-color Rainbow palette cycling, text #FFFFFF
  Sub/leaf topics: roundedRect, elbow, no forced fill (inherits sheet theme)
  Structure:       org.xmind.ui.map.clockwise
  Theme:           theme-rainbow-elbow (Rainbow-#245570-MULTI_LINE_COLORS)
"""
from __future__ import annotations
import io
import json
import time
import uuid
import zipfile

# ── Rainbow palette — exact 6 colors from xmind_template.json ────────────────
BRANCH_COLORS = [
    "#0B5D99",  # 1 classic blue
    "#534E96",  # 2 indigo
    "#7B4083",  # 3 purple
    "#A23E6A",  # 4 burgundy
    "#C34150",  # 5 ruby
    "#CA5835",  # 6 orange
]
CENTRAL_FILL  = "#245570"
CENTRAL_TEXT  = "#FFFFFF"
BRANCH_TEXT   = "#FFFFFF"
MAP_FILL      = "#FFFFFF"

SHAPE_ROUNDED  = "org.xmind.topicShape.roundedRect"
CONN_ELBOW     = "org.xmind.branchConnection.elbow"
STRUCTURE_CW   = "org.xmind.ui.map.clockwise"
LOGIC_RIGHT    = "org.xmind.ui.logic.right"

COLOR_THEME_ID = "Rainbow-#245570-MULTI_LINE_COLORS"
SKELETON_ID    = "db4a5df4db39a8cd1310ea55ea"
MULTI_LINE     = "#0B5D99 #534E96 #7B4083 #A23E6A #C34150 #CA5835"
COLOR_LIST     = "#000229 #1F2766 #52CC83 #4D86DB #99142F #245570"
MODIFIER       = "Vana 22.11.3656"


def _uid() -> str:
    return uuid.uuid4().hex


def _ts() -> str:
    return str(int(time.time() * 1000))


# ── node builders ─────────────────────────────────────────────────────────────

def _make_subtopic(node: dict) -> dict:
    """Sub-topic or leaf: minimal style — elbow + roundedRect only."""
    t: dict = {
        "id": _uid(),
        "class": "topic",
        "title": node.get("title", ""),
        "style": {
            "properties": {
                "line-class": CONN_ELBOW,
                "shape-class": SHAPE_ROUNDED,
            }
        },
    }
    children = node.get("children") or []
    if children:
        t["children"] = {
            "attached": [_make_subtopic(c) for c in children]
        }
    return t


def _make_main_branch(node: dict, color: str) -> dict:
    """Main branch: full color style from palette."""
    t: dict = {
        "id": _uid(),
        "class": "topic",
        "title": node.get("title", ""),
        "style": {
            "properties": {
                "svg:fill":    color,
                "fo:color":    BRANCH_TEXT,
                "line-color":  color,
                "line-class":  CONN_ELBOW,
                "shape-class": SHAPE_ROUNDED,
            }
        },
    }
    children = node.get("children") or []
    if children:
        t["children"] = {
            "attached": [_make_subtopic(c) for c in children]
        }
    return t


def _make_root(title: str, branches: list[dict]) -> dict:
    """Central / root topic."""
    root: dict = {
        "id": _uid(),
        "class": "topic",
        "title": title,
        "structureClass": STRUCTURE_CW,
        "style": {
            "properties": {
                "svg:fill":         CENTRAL_FILL,
                "fo:color":         CENTRAL_TEXT,
                "line-color":       CENTRAL_FILL,
                "line-class":       CONN_ELBOW,
                "shape-class":      SHAPE_ROUNDED,
                "border-line-width": "3pt",
            }
        },
        "extensions": [
            {
                "provider": "org.xmind.ui.skeleton.structure.style",
                "content": [
                    {
                        "name": "centralTopic",
                        "content": STRUCTURE_CW,
                    },
                    {
                        "name": "mainTopic",
                        "content": LOGIC_RIGHT,
                    },
                ],
            }
        ],
    }
    if branches:
        root["children"] = {
            "attached": [
                _make_main_branch(b, BRANCH_COLORS[i % len(BRANCH_COLORS)])
                for i, b in enumerate(branches)
            ]
        }
    return root


# ── theme ─────────────────────────────────────────────────────────────────────

def _build_theme() -> dict:
    """Full theme-rainbow-elbow definition from xmind_template.json."""
    base_topic = {
        "line-class":         CONN_ELBOW,
        "shape-class":        SHAPE_ROUNDED,
        "border-line-width":  "0pt",
        "fo:font-family":     "'NeverMind','sans-serif'",
        "fo:font-weight":     "500",
        "fo:text-align":      "left",
    }
    return {
        "id":             "theme-rainbow-elbow",
        "colorThemeId":   COLOR_THEME_ID,
        "skeletonThemeId": SKELETON_ID,
        "map": {
            "properties": {
                "svg:fill":         MAP_FILL,
                "multi-line-colors": MULTI_LINE,
                "color-list":       COLOR_LIST,
                "line-tapered":     "none",
            }
        },
        "centralTopic": {
            "properties": {
                **base_topic,
                "svg:fill":          CENTRAL_FILL,
                "line-color":        CENTRAL_FILL,
                "fo:color":          CENTRAL_TEXT,
                "border-line-color": CENTRAL_FILL,
                "border-line-width": "0pt",
                "line-width":        "3pt",
                "line-pattern":      "solid",
                "arrow-end-class":   "org.xmind.arrowShape.none",
                "alignment-by-level": "inactived",
                "fo:font-size":      "30pt",
                "fo:text-align":     "center",
                "fo:text-transform": "manual",
                "fo:text-decoration": "none",
            }
        },
        "mainTopic": {
            "properties": {
                **base_topic,
                "line-width":   "2pt",
                "line-pattern": "solid",
                "fo:font-size": "18pt",
            }
        },
        "subTopic": {
            "properties": {
                **base_topic,
                "line-width":   "2pt",
                "line-pattern": "solid",
                "fo:font-size": "14pt",
                "fo:font-weight": "400",
            }
        },
        "summaryTopic": {
            "properties": {
                **base_topic,
                "svg:fill":          "none",
                "border-line-color": CENTRAL_FILL,
                "fo:font-size":      "14pt",
                "fo:font-weight":    "400",
            }
        },
        "calloutTopic": {
            "properties": {
                **base_topic,
                "svg:fill":           CENTRAL_FILL,
                "border-line-color":  CENTRAL_FILL,
                "callout-shape-class": "org.xmind.calloutTopicShape.balloon.roundedRect",
                "fo:font-size":       "14pt",
                "fo:font-weight":     "400",
            }
        },
        "floatingTopic": {
            "properties": {
                **base_topic,
                "svg:fill":          "#EEEBEE",
                "border-line-color": "#EEEBEE",
                "line-width":        "2pt",
                "line-pattern":      "solid",
                "arrow-end-class":   "org.xmind.arrowShape.none",
                "fo:font-size":      "14pt",
            }
        },
        "boundary": {
            "properties": {
                "svg:fill":          CENTRAL_FILL,
                "line-color":        CENTRAL_FILL,
                "boundaryShape":     "org.xmind.boundaryShape.roundedRect",
                "fo:font-family":    "'NeverMind','sans-serif'",
                "fo:font-size":      "14pt",
                "fo:font-weight":    "400",
                "fo:text-align":     "center",
                "corner-radius":     "20pt",
                "line-width":        "2pt",
                "line-pattern":      "dash",
            }
        },
        "summary": {
            "properties": {
                "line-color":   CENTRAL_FILL,
                "summaryShape": "org.xmind.summaryShape.round",
                "line-width":   "2pt",
                "line-pattern": "solid",
                "corner-radius": "8pt",
            }
        },
        "relationship": {
            "properties": {
                "line-color":        CENTRAL_FILL,
                "relationshipShape": "org.xmind.relationshipShape.curved",
                "line-width":        "2pt",
                "line-pattern":      "dash",
                "arrow-begin-class": "org.xmind.arrowShape.none",
                "arrow-end-class":   "org.xmind.arrowShape.triangle",
                "fo:font-family":    "'NeverMind','sans-serif'",
                "fo:font-size":      "13pt",
                "fo:font-weight":    "400",
                "fo:text-align":     "center",
            }
        },
        "importantTopic": {
            "properties": {
                "svg:fill":          "#557024",
                "border-line-color": "#557024",
            }
        },
        "minorTopic": {
            "properties": {
                "svg:fill":          "#702455",
                "border-line-color": "#702455",
            }
        },
        "expiredTopic": {
            "properties": {
                "svg:fill":         "none",
                "fo:text-decoration": "line-through",
            }
        },
        "global": {
            "properties": {}
        },
    }


# ── public API ────────────────────────────────────────────────────────────────

def build_xmind(title: str, branches: list[dict]) -> bytes:
    """
    Build and return the bytes of a valid .xmind ZIP file.

    Parameters
    ----------
    title    : central topic text
    branches : list of branch dicts  {"title": str, "children": [...]}
    """
    sheet_id = _uid()

    content_json = [
        {
            "id":    sheet_id,
            "class": "sheet",
            "title": "Map 1",
            "rootTopic": _make_root(title, branches),
            "theme":     _build_theme(),
            "topicPositioning": "fixed",
            "coreVersion":      "2.99.0",
        }
    ]

    metadata_json = {
        "modifier":               MODIFIER,
        "dataStructureVersion":   "1",
        "layoutEngineVersion":    "2",
        "activeSheetId":          sheet_id,
    }

    manifest_json = {
        "file-entries": {
            "content.json": {},
            "metadata.json": {},
        }
    }

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("content.json",  json.dumps(content_json,  ensure_ascii=False))
        zf.writestr("metadata.json", json.dumps(metadata_json, ensure_ascii=False))
        zf.writestr("manifest.json", json.dumps(manifest_json, ensure_ascii=False))
    return buf.getvalue()
