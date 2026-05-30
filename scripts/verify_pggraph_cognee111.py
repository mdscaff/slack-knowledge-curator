"""Runtime verification: pgGraph adapter against cognee 1.1.1.

Mirrors the community adapter's example.py. Confirms that with cognee 1.1.1 we
can register the adapter, construct it through the factory (_GraphEngineHandle),
run CRUD + build_graph + traversal, and that pgGraph gracefully falls back to
SQL on a stock (non-pgGraph) Postgres.

Run against a throwaway Postgres (see DEVLOG). Connection comes from env:
  GRAPH_DATABASE_* and DB_*  (port overridable via PGPORT_OVERRIDE)
"""

import asyncio
import os

import cognee
from cognee.infrastructure.databases.graph import get_graph_engine

from cognee_community_graph_adapter_pggraph import register


async def main() -> int:
    print("cognee", cognee.__version__)
    register()

    port = int(os.getenv("GRAPH_DATABASE_PORT", "5434"))
    cognee.config.set_graph_database_provider("pggraph")
    cognee.config.set_graph_db_config(
        {
            "graph_database_host": os.getenv("GRAPH_DATABASE_HOST", "localhost"),
            "graph_database_port": port,
            "graph_database_name": os.getenv("GRAPH_DATABASE_NAME", "cognee"),
            "graph_database_username": os.getenv("GRAPH_DATABASE_USERNAME", "cognee"),
            "graph_database_password": os.getenv("GRAPH_DATABASE_PASSWORD", "cognee"),
        }
    )

    graph = await get_graph_engine()
    print("adapter class:", graph.__class__.__name__)
    print("pgGraph ready:", getattr(graph, "_pggraph_ready", False), "(False == SQL fallback)")

    await graph.delete_graph()
    await graph.add_nodes(
        [
            ("turing", {"name": "Alan Turing", "type": "Person"}),
            ("bletchley", {"name": "Bletchley Park", "type": "Place"}),
            ("crypto", {"name": "Cryptography", "type": "Field"}),
        ]
    )
    await graph.add_edges(
        [
            ("turing", "bletchley", "worked_at", {}),
            ("turing", "crypto", "researched", {}),
        ]
    )

    if hasattr(graph, "build_graph"):
        print("build_graph:", await graph.build_graph())

    neighbors = await graph.get_neighbors("turing")
    names = sorted(n.get("name") for n in neighbors)
    print("neighbors of turing:", names)

    nodes, edges = await graph.get_neighborhood(["turing"], depth=2)
    print("2-hop node ids:", sorted(n[0] for n in nodes))
    print("edges in subgraph:", len(edges))

    ok = set(names) == {"Bletchley Park", "Cryptography"} and len(edges) == 2
    print("\nRESULT:", "PASS ✅" if ok else "FAIL ❌")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
