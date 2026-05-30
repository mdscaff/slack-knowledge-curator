"""Interactive HTML visualization of the knowledge graph.

Centers on the **top N entities** (by degree) and their entity↔entity semantic
relationships, with a **toggle switch per top entity** (show/hide it + its links)
and **click-to-source**: clicking any node lists the saved posts/articles that
mention that entity, as clickable links.

Provenance: each Cognee entity was extracted from a document we built per saved
item, so we map an entity back to its source items (by the per-item entities/tags
Claude assigned, with a whole-word text fallback) and surface those items' links.

Reads the Postgres graph (asyncpg) + the classified items. Runs in the cognee
venv. Output: data/viz/<channel>.html (gitignored — built from private content).
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from pathlib import Path

from ..config import Settings
from ..models import Item
from ..store import read_items

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


async def _load_graph(top_n: int, neighbors_per: int):
    import asyncpg

    conn = await asyncpg.connect(_dsn())
    try:
        top = await conn.fetch(
            """
            SELECT n.id, n.name, count(e.*) AS deg
            FROM graph_node n
            JOIN graph_edge e ON (e.source_id = n.id OR e.target_id = n.id)
            WHERE n.type = 'Entity'
            GROUP BY n.id, n.name ORDER BY deg DESC LIMIT $1
            """,
            top_n,
        )
        top_ids = [r["id"] for r in top]
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
        degree = {
            r["id"]: r["deg"]
            for r in await conn.fetch(
                """
                SELECT n.id, count(e.*) AS deg FROM graph_node n
                JOIN graph_edge e ON (e.source_id = n.id OR e.target_id = n.id)
                WHERE n.type = 'Entity' GROUP BY n.id
                """
            )
        }
        top_set = set(top_ids)
        cand: dict[str, list[str]] = {tid: [] for tid in top_ids}
        for e in inc:
            s, t = e["source_id"], e["target_id"]
            if s in top_set and t not in top_set:
                cand[s].append(t)
            elif t in top_set and s not in top_set:
                cand[t].append(s)
        keep: set[str] = set()
        for tid, nbrs in cand.items():
            keep.update(sorted(set(nbrs), key=lambda x: -degree.get(x, 0))[:neighbors_per])
        node_ids = top_set | keep
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


def _item_keys(item: Item) -> set[str]:
    keys: set[str] = set()
    if item.classification:
        for e in item.classification.entities:
            keys.add(e.name.lower().strip())
        for t in item.classification.tags:
            keys.add(t.lower().strip())
    return keys


def _item_text(item: Item) -> str:
    parts = [item.text]
    for e in item.enrichment.values():
        parts += [e.title or "", e.text or "", e.author or ""]
    if item.classification:
        parts.append(item.classification.summary)
    return " ".join(parts).lower()


def _source_label(item: Item, url: str) -> str:
    e = item.enrichment.get(url)
    if e and e.type == "x_post" and e.author:
        snippet = (e.text or "").strip().replace("\n", " ")
        return f"@{e.author}" + (f": {snippet[:70]}…" if snippet else "")
    if e and e.title:
        return e.title[:90]
    if item.classification and item.classification.summary:
        return item.classification.summary[:90]
    return url


def _sources_for(name: str, items, keys_by_id, text_by_id) -> list[dict]:
    key = name.lower().strip()
    matched = [it for it in items if key in keys_by_id[it.id]]
    if not matched and len(key) >= 4:
        pat = re.compile(r"\b" + re.escape(key) + r"\b")
        matched = [it for it in items if pat.search(text_by_id[it.id])]
    out: list[dict] = []
    seen: set[str] = set()
    for it in matched:
        for url in it.links:
            if url in seen:
                continue
            seen.add(url)
            out.append({"url": url, "label": _source_label(it, url)})
            break  # one primary link per item
        if len(out) >= 12:
            break
    return out


def _build_html(top, top_ids, node_ids, edges, names, items) -> str:
    color_of = {tid: _PALETTE[i % len(_PALETTE)] for i, tid in enumerate(top_ids)}
    top_set = set(top_ids)
    keys_by_id = {it.id: _item_keys(it) for it in items}
    text_by_id = {it.id: _item_text(it) for it in items}

    links: dict[str, set] = {nid: set() for nid in node_ids}
    for s, t, _ in edges:
        if s in top_set:
            links[t].add(s)
        if t in top_set:
            links[s].add(t)

    nodes_js, sources = [], {}
    for nid in node_ids:
        is_top = nid in top_set
        tops = [nid] if is_top else sorted(links.get(nid, []))
        primary = nid if is_top else (tops[0] if tops else None)
        name = names.get(nid, nid)
        nodes_js.append(
            {
                "id": nid, "label": name, "isTop": is_top, "tops": tops,
                "color": color_of.get(primary, "#c9c9c9") if primary else "#c9c9c9",
                "size": 34 if is_top else 14, "font": {"size": 20 if is_top else 12},
            }
        )
        sources[nid] = _sources_for(name, items, keys_by_id, text_by_id)
    edges_js = [{"from": s, "to": t, "label": rel} for s, t, rel in edges]
    toggles = [
        {"id": r["id"], "label": names.get(r["id"], r["id"]), "deg": r["deg"],
         "color": color_of[r["id"]]}
        for r in top
    ]
    data = json.dumps(
        {"nodes": nodes_js, "edges": edges_js, "toggles": toggles, "sources": sources}
    )
    return _TEMPLATE.replace("/*DATA*/", data)


async def _build(settings: Settings, channel: str, top_n: int, neighbors_per: int) -> Path:
    top, top_ids, node_ids, edges, names = await _load_graph(top_n, neighbors_per)
    if not top:
        raise RuntimeError(
            "No entities in the graph. Build it first: `skc graph --channel <id>`."
        )
    items = [it for it in read_items(settings.classified_dir / f"{channel}.jsonl")]
    html = _build_html(top, top_ids, node_ids, edges, names, items)
    out = settings.data_dir / "viz" / f"{channel}.html"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html, encoding="utf-8")
    return out


def build_visualization(settings: Settings, channel: str, *, top_n: int = 8, neighbors_per: int = 12) -> Path:
    try:
        import asyncpg  # noqa: F401
    except ImportError as exc:
        raise RuntimeError(
            "Visualization needs asyncpg (the graph extra): uv pip install -e '.[graph]'"
        ) from exc
    return asyncio.run(_build(settings, channel, top_n, neighbors_per))


_TEMPLATE = r"""<!doctype html>
<meta charset="utf-8">
<title>Knowledge Graph — top entities</title>
<script src="https://unpkg.com/vis-network/standalone/umd/vis-network.min.js"></script>
<style>
  :root { color-scheme: light; }
  body { margin:0; font:14px/1.4 -apple-system,Segoe UI,Roboto,sans-serif; }
  #wrap { display:flex; height:100vh; }
  #panel { width:320px; padding:18px 20px; border-right:1px solid #eee; overflow:auto; }
  #panel h1 { font-size:1.05rem; margin:0 0 4px; }
  #panel p { color:#666; margin:0 0 16px; font-size:.85rem; }
  #graph { flex:1; }
  .row { display:flex; align-items:center; justify-content:space-between; padding:7px 0; }
  .ent { display:flex; align-items:center; gap:9px; font-weight:600; }
  .dot { width:12px; height:12px; border-radius:50%; flex:none; }
  .deg { color:#999; font-weight:400; font-size:.8rem; margin-left:4px; }
  .sw { position:relative; width:40px; height:22px; flex:none; }
  .sw input { opacity:0; width:0; height:0; }
  .sl { position:absolute; inset:0; background:#ccc; border-radius:22px; transition:.2s; cursor:pointer; }
  .sl:before { content:""; position:absolute; height:16px; width:16px; left:3px; bottom:3px;
               background:#fff; border-radius:50%; transition:.2s; }
  .sw input:checked + .sl { background:#34c759; }
  .sw input:checked + .sl:before { transform:translateX(18px); }
  #all { margin:14px 0 0; font-size:.8rem; color:#4363d8; cursor:pointer; background:none; border:none; padding:0; }
  #sources { margin-top:20px; border-top:1px solid #eee; padding-top:14px; }
  #sources h2 { font-size:.95rem; margin:0 0 2px; }
  #srcHint { color:#999; font-size:.8rem; }
  #srcList { list-style:none; padding:0; margin:10px 0 0; }
  #srcList li { margin:0 0 10px; font-size:.85rem; line-height:1.35; }
  #srcList a { color:#4363d8; text-decoration:none; word-break:break-word; }
  #srcList a:hover { text-decoration:underline; }
  #srcList .host { display:block; color:#aaa; font-size:.72rem; }
</style>
<div id="wrap">
  <div id="panel">
    <h1>Knowledge Graph</h1>
    <p>Top entities by connections. Toggle to show/hide; <b>click a node</b> for sources.</p>
    <div id="toggles"></div>
    <button id="all">reset all on</button>
    <div id="sources">
      <h2 id="srcTitle">Sources</h2>
      <div id="srcHint">Click any node to see the saved posts/articles it came from.</div>
      <ul id="srcList"></ul>
    </div>
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
    interaction:{ hover:true, tooltipDelay:120 }, nodes:{ borderWidth:0 } });

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
  e.target.checked ? active.add(id) : active.delete(id); refresh();
});
document.getElementById("all").onclick = () => {
  G.toggles.forEach(t => active.add(t.id));
  document.querySelectorAll("#toggles input").forEach(i => i.checked = true); refresh();
};

function hostOf(u){ try { return new URL(u).hostname.replace(/^www\./,""); } catch(e){ return ""; } }
function showSources(nodeId){
  const n = allNodes.get(nodeId);
  document.getElementById("srcTitle").textContent = n ? n.label : "Sources";
  const list = document.getElementById("srcList");
  const hint = document.getElementById("srcHint");
  const srcs = G.sources[nodeId] || [];
  list.innerHTML = "";
  if(!srcs.length){ hint.textContent = "No source links found for this entity."; return; }
  hint.textContent = `${srcs.length} saved item${srcs.length>1?"s":""} mention this:`;
  srcs.forEach(s => {
    const li = document.createElement("li");
    li.innerHTML = `<a href="${s.url}" target="_blank" rel="noopener">${s.label}</a>`
      + `<span class="host">${hostOf(s.url)}</span>`;
    list.appendChild(li);
  });
}
network.on("click", p => { if(p.nodes.length) showSources(p.nodes[0]); });
refresh();
</script>
"""
