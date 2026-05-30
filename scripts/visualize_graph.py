"""Render an interactive HTML visualization of the knowledge graph.

Centers on the **top N entities** (by degree) and their entity↔entity semantic
relationships, and emits a self-contained HTML page (vis-network via CDN) with a
**toggle switch per top entity** to show/hide it and its connections live.

Reads the Postgres graph the Cognee/pgGraph stage built. Run with the cognee venv
(it has asyncpg):

  .venv-cognee/bin/python scripts/visualize_graph.py [TOP_N]

Output: data/viz/graph.html  (gitignored — built from your private content).
"""

import asyncio
import json
import os
import sys
from pathlib import Path

import asyncpg

# Structural edges connect entities to chunks/types/sets — noise for a concept
# map. We keep only entity↔entity *semantic* relationships.
_STRUCTURAL = {"belongs_to_set", "contains", "is_a"}
_PALETTE = [
    "#e6194B", "#3cb44b", "#4363d8", "#f58231",
    "#911eb4", "#42d4f4", "#f032e6", "#bfef45",
]


def _dsn() -> str:
    return (
        f"postgresql://{os.getenv('DB_USERNAME','cognee')}:{os.getenv('DB_PASSWORD','cognee')}"
        f"@{os.getenv('DB_HOST','localhost')}:{os.getenv('DB_PORT','5434')}"
        f"/{os.getenv('DB_NAME','cognee')}"
    )


async def _load(top_n: int, neighbors_per: int):
    conn = await asyncpg.connect(_dsn())
    try:
        top = await conn.fetch(
            """
            SELECT n.id, n.name, count(e.*) AS deg
            FROM graph_node n
            JOIN graph_edge e ON (e.source_id = n.id OR e.target_id = n.id)
            WHERE n.type = 'Entity'
            GROUP BY n.id, n.name
            ORDER BY deg DESC
            LIMIT $1
            """,
            top_n,
        )
        top_ids = [r["id"] for r in top]

        # Entity↔entity semantic edges incident to a top entity.
        inc = await conn.fetch(
            """
            SELECT e.source_id, e.target_id, e.relationship_name
            FROM graph_edge e
            JOIN graph_node s ON s.id = e.source_id AND s.type = 'Entity'
            JOIN graph_node t ON t.id = e.target_id AND t.type = 'Entity'
            WHERE e.relationship_name <> ALL($2::text[])
              AND (e.source_id = ANY($1) OR e.target_id = ANY($1))
            """,
            top_ids,
            list(_STRUCTURAL),
        )

        # Global entity degree (to pick the strongest neighbors).
        deg_rows = await conn.fetch(
            """
            SELECT n.id, count(e.*) AS deg
            FROM graph_node n
            JOIN graph_edge e ON (e.source_id = n.id OR e.target_id = n.id)
            WHERE n.type = 'Entity'
            GROUP BY n.id
            """
        )
        degree = {r["id"]: r["deg"] for r in deg_rows}

        # Pick up to `neighbors_per` neighbors for each top entity, by their degree.
        top_set = set(top_ids)
        cand: dict[str, list[str]] = {tid: [] for tid in top_ids}
        for e in inc:
            s, t = e["source_id"], e["target_id"]
            if s in top_set and t not in top_set:
                cand[s].append(t)
            elif t in top_set and s not in top_set:
                cand[t].append(s)
        keep_neighbors: set[str] = set()
        for tid, nbrs in cand.items():
            ranked = sorted(set(nbrs), key=lambda x: -degree.get(x, 0))[:neighbors_per]
            keep_neighbors.update(ranked)

        node_ids = top_set | keep_neighbors

        # All semantic edges among the kept node set.
        edges = [
            (e["source_id"], e["target_id"], e["relationship_name"])
            for e in inc
            if e["source_id"] in node_ids and e["target_id"] in node_ids
        ]

        names = {
            r["id"]: r["name"]
            for r in await conn.fetch(
                "SELECT id, name FROM graph_node WHERE id = ANY($1)", list(node_ids)
            )
        }
        return top, top_ids, node_ids, edges, names
    finally:
        await conn.close()


def _build_html(top, top_ids, node_ids, edges, names) -> str:
    color_of = {tid: _PALETTE[i % len(_PALETTE)] for i, tid in enumerate(top_ids)}
    top_set = set(top_ids)

    # Which top entities each node connects to (drives toggle-based visibility).
    links: dict[str, set] = {nid: set() for nid in node_ids}
    for s, t, _ in edges:
        if s in top_set:
            links[t].add(s)
        if t in top_set:
            links[s].add(t)

    nodes_js = []
    for nid in node_ids:
        is_top = nid in top_set
        tops = [nid] if is_top else sorted(links.get(nid, []))
        primary = nid if is_top else (tops[0] if tops else None)
        nodes_js.append(
            {
                "id": nid,
                "label": names.get(nid, nid),
                "isTop": is_top,
                "tops": tops,
                "color": color_of.get(primary, "#c9c9c9") if primary else "#c9c9c9",
                "size": 34 if is_top else 14,
                "font": {"size": 20 if is_top else 12},
            }
        )
    edges_js = [{"from": s, "to": t, "label": rel} for s, t, rel in edges]
    toggles = [
        {"id": r["id"], "label": names.get(r["id"], r["id"]), "deg": r["deg"],
         "color": color_of[r["id"]]}
        for r in top
    ]

    data = json.dumps({"nodes": nodes_js, "edges": edges_js, "toggles": toggles})
    return _TEMPLATE.replace("/*DATA*/", data)


_TEMPLATE = r"""<!doctype html>
<meta charset="utf-8">
<title>Knowledge Graph — top entities</title>
<script src="https://unpkg.com/vis-network/standalone/umd/vis-network.min.js"></script>
<style>
  :root { color-scheme: light; }
  body { margin:0; font:14px/1.4 -apple-system,Segoe UI,Roboto,sans-serif; }
  #wrap { display:flex; height:100vh; }
  #panel { width:300px; padding:18px 20px; border-right:1px solid #eee; overflow:auto; }
  #panel h1 { font-size:1.05rem; margin:0 0 4px; }
  #panel p { color:#666; margin:0 0 16px; font-size:.85rem; }
  #graph { flex:1; }
  .row { display:flex; align-items:center; justify-content:space-between; padding:7px 0; }
  .ent { display:flex; align-items:center; gap:9px; font-weight:600; }
  .dot { width:12px; height:12px; border-radius:50%; flex:none; }
  .deg { color:#999; font-weight:400; font-size:.8rem; margin-left:4px; }
  /* toggle switch */
  .sw { position:relative; width:40px; height:22px; flex:none; }
  .sw input { opacity:0; width:0; height:0; }
  .sl { position:absolute; inset:0; background:#ccc; border-radius:22px; transition:.2s; cursor:pointer; }
  .sl:before { content:""; position:absolute; height:16px; width:16px; left:3px; bottom:3px;
               background:#fff; border-radius:50%; transition:.2s; }
  .sw input:checked + .sl { background:#34c759; }
  .sw input:checked + .sl:before { transform:translateX(18px); }
  #all { margin-top:14px; font-size:.8rem; color:#4363d8; cursor:pointer; background:none; border:none; padding:0; }
</style>
<div id="wrap">
  <div id="panel">
    <h1>Knowledge Graph</h1>
    <p>Top entities by connections. Toggle each to show/hide it and its links.</p>
    <div id="toggles"></div>
    <button id="all">reset all on</button>
  </div>
  <div id="graph"></div>
</div>
<script>
const G = /*DATA*/;
const active = new Set(G.toggles.map(t => t.id));

const allNodes = new vis.DataSet(G.nodes.map(n => ({...n, shape:"dot"})));
const allEdges = new vis.DataSet(G.edges.map((e,i) => ({...e, id:i, arrows:"to",
  color:{color:"#d0d0d0",opacity:.55}, font:{size:10,color:"#999",strokeWidth:3}})));

function visibleNode(n){ return n.isTop ? active.has(n.id) : n.tops.some(t => active.has(t)); }
function refresh(){
  const vis_ids = new Set();
  allNodes.forEach(n => { const v = visibleNode(n); allNodes.update({id:n.id, hidden:!v}); if(v) vis_ids.add(n.id); });
  allEdges.forEach(e => allEdges.update({id:e.id, hidden:!(vis_ids.has(e.from)&&vis_ids.has(e.to))}));
}

const network = new vis.Network(document.getElementById("graph"),
  {nodes:allNodes, edges:allEdges},
  { physics:{ stabilization:true, barnesHut:{ gravitationalConstant:-12000, springLength:140 } },
    interaction:{ hover:true, tooltipDelay:120 },
    nodes:{ borderWidth:0, shadow:false } });

const box = document.getElementById("toggles");
G.toggles.forEach(t => {
  const row = document.createElement("div"); row.className = "row";
  row.innerHTML = `<span class="ent"><span class="dot" style="background:${t.color}"></span>`
    + `${t.label}<span class="deg">${t.deg}</span></span>`
    + `<label class="sw"><input type="checkbox" checked data-id="${t.id}"><span class="sl"></span></label>`;
  box.appendChild(row);
});
box.addEventListener("change", e => {
  const id = e.target.getAttribute("data-id"); if(!id) return;
  e.target.checked ? active.add(id) : active.delete(id);
  refresh();
});
document.getElementById("all").onclick = () => {
  G.toggles.forEach(t => active.add(t.id));
  document.querySelectorAll("#toggles input").forEach(i => i.checked = true);
  refresh();
};
refresh();
</script>
"""


async def main(top_n: int) -> int:
    top, top_ids, node_ids, edges, names = await _load(top_n, neighbors_per=12)
    if not top:
        print("No entities found — has the graph been built? (skc graph)", file=sys.stderr)
        return 1
    html = _build_html(top, top_ids, node_ids, edges, names)
    out = Path(os.getenv("DATA_DIR", "./data")) / "viz" / "graph.html"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html, encoding="utf-8")
    print(f"top {len(top)} entities: " + ", ".join(f"{r['name']}({r['deg']})" for r in top))
    print(f"{len(node_ids)} nodes, {len(edges)} edges → {out}")
    return 0


if __name__ == "__main__":
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 8
    raise SystemExit(asyncio.run(main(n)))
