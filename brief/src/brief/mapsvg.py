"""The map drawn without a browser: `map.svg` (old tablets, e-reader, HA picture card) and `map.drawio`.

`place` gives every node a centre point: the saved default layout wins, and only nodes without a saved
position are placed automatically (in tiers by kind, containers inside their server), so an arrangement
you made is never reshuffled. Both outputs draw from the same document `mapdata.build_map` returns.
"""

import math
from collections.abc import Mapping
from xml.sax.saxutils import escape, quoteattr

NODE_W, NODE_H = 150, 40
CELL_W, CELL_H = 140, 46
GAP_X, GAP_Y = 40, 110
MARGIN = 50
TIERS = (
    ("internet",),
    ("gateway",),
    ("switch",),
    ("ap", "server", "service"),
    ("camera", "iot_group", "client_group", "display", "ups", "other"),
)
STATE_FILL = {"green": "#2f9e44", "yellow": "#f59f00", "red": "#e03131", "unknown": "#868e96"}
EDGE_STYLE = {
    "uplink": ("#495057", ""),
    "depends": ("#1c7ed6", "6 4"),
    "power": ("#c2255c", "2 4"),
    "hosts": ("#adb5bd", ""),
}


def _tier(kind: str) -> int:
    for i, kinds in enumerate(TIERS):
        if kind in kinds:
            return i
    return len(TIERS) - 1


def _children(doc: Mapping) -> dict[str, list[dict]]:
    kids: dict[str, list[dict]] = {}
    for n in doc["nodes"]:
        if n.get("parent"):
            kids.setdefault(n["parent"], []).append(n)
    return kids


def sizes(doc: Mapping) -> dict[str, tuple[float, float]]:
    """Width and height of every node; a node holding others becomes a box around them."""
    kids = _children(doc)
    out = {}
    for n in doc["nodes"]:
        held = kids.get(n["id"])
        if held:
            cols = max(1, math.ceil(math.sqrt(len(held))))
            rows = math.ceil(len(held) / cols)
            out[n["id"]] = (max(NODE_W, cols * CELL_W + 20), 40 + rows * CELL_H + 10)
        else:
            out[n["id"]] = (NODE_W, NODE_H)
    return out


def place(doc: Mapping) -> dict[str, tuple[float, float]]:
    """Centre point of every node: the saved layout where there is one, automatic placement elsewhere."""
    saved = doc.get("layout") or {}
    size = sizes(doc)
    kids = _children(doc)
    pos: dict[str, tuple[float, float]] = {}

    tops = [n for n in doc["nodes"] if not n.get("parent")]
    tiers: dict[int, list[dict]] = {}
    for n in sorted(tops, key=lambda n: (_tier(n["kind"]), n["label"].lower())):
        tiers.setdefault(_tier(n["kind"]), []).append(n)
    y = MARGIN
    widest = max((sum(size[n["id"]][0] + GAP_X for n in row) for row in tiers.values()), default=0)
    for tier in sorted(tiers):
        row = tiers[tier]
        height = max(size[n["id"]][1] for n in row)
        x = MARGIN + (widest - sum(size[n["id"]][0] + GAP_X for n in row)) / 2
        for n in row:
            w, h = size[n["id"]]
            pos[n["id"]] = (x + w / 2, y + height / 2)
            x += w + GAP_X
        y += height + GAP_Y

    for parent, held in kids.items():
        if parent not in pos:
            continue
        px, py = pos[parent]
        w, h = size[parent]
        cols = max(1, math.ceil(math.sqrt(len(held))))
        for i, n in enumerate(sorted(held, key=lambda n: n["label"].lower())):
            col, row = i % cols, i // cols
            pos[n["id"]] = (
                px - w / 2 + 10 + CELL_W / 2 + col * CELL_W,
                py - h / 2 + 40 + CELL_H / 2 + row * CELL_H,
            )

    for node_id, p in saved.items():
        if node_id in size:
            pos[node_id] = (float(p["x"]), float(p["y"]))
    for n in doc["nodes"]:  # a child whose parent was never placed
        pos.setdefault(n["id"], (MARGIN, MARGIN))
    return pos


def _visible(doc: Mapping) -> tuple[list[dict], list[dict]]:
    layers = set(doc.get("layers") or ())
    nodes = [n for n in doc["nodes"] if n.get("layer") in layers or n.get("parent")]
    ids = {n["id"] for n in nodes}
    edges = [e for e in doc["edges"] if e["layer"] in layers and e["src"] in ids and e["dst"] in ids]
    return nodes, edges


def render_svg(doc: Mapping, width: int = 1600, height: int = 1000) -> str:
    nodes, edges = _visible(doc)
    view = {**doc, "nodes": nodes}
    pos, size = place(view), sizes(view)
    kids = _children(view)
    xs = [pos[n["id"]][0] - size[n["id"]][0] / 2 for n in nodes] or [0]
    ys = [pos[n["id"]][1] - size[n["id"]][1] / 2 for n in nodes] or [0]
    xe = [pos[n["id"]][0] + size[n["id"]][0] / 2 for n in nodes] or [100]
    ye = [pos[n["id"]][1] + size[n["id"]][1] / 2 for n in nodes] or [100]
    x0, y0 = min(xs) - MARGIN, min(ys) - MARGIN
    w, h = max(xe) - x0 + MARGIN, max(ye) - y0 + MARGIN + 24

    out = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="{x0:.0f} {y0:.0f} {w:.0f} {h:.0f}" font-family="DejaVu Sans, sans-serif" font-size="12">',
        f'<rect x="{x0:.0f}" y="{y0:.0f}" width="{w:.0f}" height="{h:.0f}" fill="#ffffff"/>',
    ]
    if not doc.get("run_ok", True):
        out.append(
            f'<text x="{x0 + 12:.0f}" y="{y0 + 22:.0f}" fill="#e03131">No data today: colours are not current.</text>'
        )
    for e in edges:
        if e["kind"] == "hosts":
            continue  # containment is drawn as the box
        colour, dash = EDGE_STYLE.get(e["kind"], EDGE_STYLE["uplink"])
        (ax, ay), (bx, by) = pos[e["src"]], pos[e["dst"]]
        dash_attr = f' stroke-dasharray="{dash}"' if dash else ""
        out.append(
            f'<line x1="{ax:.0f}" y1="{ay:.0f}" x2="{bx:.0f}" y2="{by:.0f}" stroke="{colour}" stroke-width="2"{dash_attr}/>'
        )
    for n in sorted(nodes, key=lambda n: bool(n.get("parent"))):  # boxes first, their contents on top
        cx, cy = pos[n["id"]]
        nw, nh = size[n["id"]]
        fill = STATE_FILL.get(n.get("state"), STATE_FILL["unknown"])
        holder = n["id"] in kids
        rect = (
            f'<rect x="{cx - nw / 2:.0f}" y="{cy - nh / 2:.0f}" width="{nw:.0f}" height="{nh:.0f}" rx="8" '
            + (
                f'fill="#f1f3f5" stroke="{fill}" stroke-width="3"'
                if holder
                else f'fill="{fill}" stroke="#212529"'
            )
            + "/>"
        )
        label_y = cy - nh / 2 + 22 if holder else cy + 4
        colour = "#212529" if holder else "#ffffff"
        text = escape(_clip(n["label"], 22))
        badge = _badge_text(n)
        out.append(
            f'<g>{rect}<text x="{cx:.0f}" y="{label_y:.0f}" text-anchor="middle" fill="{colour}">{text}</text>'
        )
        if badge:
            out.append(
                f'<text x="{cx + nw / 2 - 6:.0f}" y="{cy - nh / 2 + 14:.0f}" text-anchor="end" fill="#e03131" '
                f'font-size="11">{escape(badge)}</text>'
            )
        out.append("</g>")
    out.append("</svg>")
    return "\n".join(out)


def _badge_text(n: Mapping) -> str:
    bits = []
    for b in n.get("badges") or []:
        if b == "new":
            bits.append("NEW")
        elif b == "update":
            bits.append("UPDATE")
        elif b.startswith("findings:"):
            bits.append("! " + b.split(":", 1)[1])
    return " ".join(bits)


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def render_drawio(doc: Mapping) -> str:
    """A plain diagrams.net file (uncompressed XML) with the same nodes, positions, and links."""
    nodes, edges = _visible(doc)
    view = {**doc, "nodes": nodes}
    pos, size = place(view), sizes(view)
    ids = {n["id"]: f"n{i + 2}" for i, n in enumerate(nodes)}
    cells = ['<mxCell id="0"/>', '<mxCell id="1" parent="0"/>']
    for n in nodes:
        cx, cy = pos[n["id"]]
        w, h = size[n["id"]]
        fill = STATE_FILL.get(n.get("state"), STATE_FILL["unknown"])
        label = quoteattr(n["label"])
        cells.append(
            f'<mxCell id="{ids[n["id"]]}" value={label} style="rounded=1;whiteSpace=wrap;html=0;'
            f'fillColor={fill};fontColor=#ffffff;" vertex="1" parent="1">'
            f'<mxGeometry x="{cx - w / 2:.0f}" y="{cy - h / 2:.0f}" width="{w:.0f}" height="{h:.0f}" as="geometry"/></mxCell>'
        )
    for i, e in enumerate(edges):
        if e["kind"] == "hosts":
            continue
        dash = "dashed=1;" if e["kind"] in ("depends", "power") else ""
        cells.append(
            f'<mxCell id="e{i}" value={quoteattr(e.get("label") or "")} style="endArrow=none;{dash}" '
            f'edge="1" parent="1" source="{ids[e["src"]]}" target="{ids[e["dst"]]}">'
            '<mxGeometry relative="1" as="geometry"/></mxCell>'
        )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n<mxfile><diagram name="Network"><mxGraphModel>'
        "<root>" + "".join(cells) + "</root></mxGraphModel></diagram></mxfile>\n"
    )
