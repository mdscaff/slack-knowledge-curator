"""Build a Cognee knowledge graph from classified items, on the pgGraph backend.

This is the showcase stage: we hand Cognee rich documents (the saved note + the
resolved link content + our Claude summary/categories/tags/entities) and let
Cognee's `cognify` pipeline extract entities and relationships into a knowledge
graph — stored in Postgres via the community **pgGraph** graph adapter, with
pgvector for embeddings. Then `skc query` runs graph-aware retrieval over it.

All three stores (relational, vector, graph) live in one Postgres. The pgGraph
adapter accelerates traversal when the `graph` extension is present and falls
back to SQL otherwise (so it works on a stock pgvector image too).

Cognee + the adapter are an optional install:  `uv pip install -e '.[graph]'`.
"""

from __future__ import annotations

import os

from rich.console import Console

from ..config import Settings
from ..store import read_items

console = Console()


def _require_cognee():
    try:
        import cognee  # noqa: F401
        from cognee_community_graph_adapter_pggraph import register  # noqa: F401
    except ImportError as exc:
        raise RuntimeError(
            "The graph stage needs Cognee + the pgGraph adapter. Install the extra:\n"
            "  uv pip install -e '.[graph]'"
        ) from exc
    return None


def _pg_url(host: str, port: str, name: str, user: str, pw: str) -> str:
    return f"postgresql+asyncpg://{user}:{pw}@{host}:{port}/{name}"


def _configure_cognee(settings: Settings):
    """Point Cognee at the all-Postgres + pgGraph backend, reading config from .env."""
    from dotenv import dotenv_values, load_dotenv

    # A blank exported var (e.g. ANTHROPIC_API_KEY='') shadows .env AND is read
    # directly by litellm/cognee, breaking LLM/embedding calls. Drop blanks for
    # any key defined in .env so the real value loads below.
    for k in dotenv_values():
        if os.environ.get(k, None) == "":
            del os.environ[k]
    load_dotenv()  # ensure Cognee (which reads os.environ) sees LLM_*/EMBEDDING_*/DB_*

    import cognee
    from cognee_community_graph_adapter_pggraph import register

    g = {
        "host": os.getenv("GRAPH_DATABASE_HOST", "localhost"),
        "port": os.getenv("GRAPH_DATABASE_PORT", "5433"),
        "name": os.getenv("GRAPH_DATABASE_NAME", "cognee"),
        "user": os.getenv("GRAPH_DATABASE_USERNAME", "cognee"),
        "pw": os.getenv("GRAPH_DATABASE_PASSWORD", "cognee"),
    }
    register()
    cognee.config.set_graph_database_provider("pggraph")
    cognee.config.set_graph_db_config(
        {
            "graph_database_provider": "pggraph",
            "graph_database_host": g["host"],
            "graph_database_port": int(g["port"]),
            "graph_database_name": g["name"],
            "graph_database_username": g["user"],
            "graph_database_password": g["pw"],
        }
    )

    # Relational + vector share the same Postgres.
    cognee.config.set_relational_db_config(
        {
            "db_provider": "postgres",
            "db_host": os.getenv("DB_HOST", g["host"]),
            "db_port": os.getenv("DB_PORT", g["port"]),
            "db_name": os.getenv("DB_NAME", g["name"]),
            "db_username": os.getenv("DB_USERNAME", g["user"]),
            "db_password": os.getenv("DB_PASSWORD", g["pw"]),
        }
    )
    cognee.config.set_vector_db_provider(os.getenv("VECTOR_DB_PROVIDER", "pgvector"))
    cognee.config.set_vector_db_url(
        _pg_url(
            os.getenv("DB_HOST", g["host"]),
            os.getenv("DB_PORT", g["port"]),
            os.getenv("DB_NAME", g["name"]),
            os.getenv("DB_USERNAME", g["user"]),
            os.getenv("DB_PASSWORD", g["pw"]),
        )
    )

    # LLM (Anthropic) + embeddings (OpenAI).
    cognee.config.set_llm_provider(os.getenv("LLM_PROVIDER", "anthropic"))
    cognee.config.set_llm_model(os.getenv("LLM_MODEL", "claude-haiku-4-5"))

    # Resolve the LLM key robustly: prefer the real ANTHROPIC_API_KEY (via
    # Settings, which drops blank shadows), fall back to LLM_API_KEY — but never
    # an unfilled placeholder. Mirror it into the env vars litellm reads.
    def _real(v: str | None) -> str:
        return v if v and "your-key-here" not in v else ""

    llm_key = _real(settings.anthropic_api_key) or _real(os.getenv("LLM_API_KEY"))
    if llm_key:
        cognee.config.set_llm_api_key(llm_key)
        os.environ["LLM_API_KEY"] = llm_key
        os.environ["ANTHROPIC_API_KEY"] = llm_key

    cognee.config.set_embedding_provider(os.getenv("EMBEDDING_PROVIDER", "openai"))
    cognee.config.set_embedding_model(os.getenv("EMBEDDING_MODEL", "text-embedding-3-small"))
    emb_key = _real(os.getenv("OPENAI_API_KEY")) or _real(os.getenv("EMBEDDING_API_KEY"))
    if emb_key:
        cognee.config.set_embedding_api_key(emb_key)
        os.environ["OPENAI_API_KEY"] = emb_key

    return cognee


def _doc_for(item) -> str:
    """A rich text record for one item — the input Cognee turns into graph nodes."""
    c = item.classification
    lines: list[str] = []
    if c and c.summary:
        lines.append(f"Summary: {c.summary}")
    if item.text.strip():
        lines.append(f"Saved note: {item.text.strip()}")
    for url, e in item.enrichment.items():
        if e.type == "x_post" and (e.text or e.author):
            lines.append(f"X post by @{e.author or 'unknown'}: {e.text or ''}")
        elif e.title or e.text:
            ref = " — ".join(b for b in (e.title, e.text) if b)
            lines.append(f"Reference: {ref} [{url}]")
    if c and c.categories:
        lines.append("Categories: " + ", ".join(c.categories))
    if c and c.tags:
        lines.append("Tags: " + ", ".join(c.tags))
    if c and c.entities:
        lines.append("Entities: " + ", ".join(f"{en.name} ({en.type})" for en in c.entities))
    return "\n".join(lines)


def _domains(item) -> list[str]:
    """Top-level category domains, used as Cognee node_set tags (filterable)."""
    if not item.classification:
        return []
    return sorted({c.split("/")[0].strip() for c in item.classification.categories if c})


async def _build(settings: Settings, channel: str, limit: int | None, reset: bool) -> None:
    cognee = _configure_cognee(settings)

    src = settings.classified_dir / f"{channel}.jsonl"
    items = [it for it in read_items(src) if it.classification]
    if not items:
        raise RuntimeError(
            f"No classified items for {channel}. Run `skc classify --channel {channel}` first."
        )
    if limit:
        items = items[:limit]

    if reset:
        console.print("  pruning existing Cognee data + system…")
        await cognee.prune.prune_data()
        await cognee.prune.prune_system()

    console.print(f"  adding {len(items)} documents to Cognee…")
    for it in items:
        await cognee.add(_doc_for(it), node_set=_domains(it) or None)

    console.print("  running [bold]cognify[/] — extracting entities + relationships…")
    await cognee.cognify()

    # Materialize the pgGraph traversal index and report acceleration status.
    from cognee.infrastructure.databases.graph import get_graph_engine

    graph = await get_graph_engine()
    ready = getattr(graph, "_pggraph_ready", False)
    if hasattr(graph, "build_graph"):
        await graph.build_graph()
    console.print(
        f"  [green]graph built[/] · adapter={graph.__class__.__name__} · "
        f"pgGraph {'accelerated' if ready else 'SQL-fallback'}"
    )


async def _query(
    settings: Settings, text: str, query_type: str, top_k: int, domain: str | None
):
    cognee = _configure_cognee(settings)
    from cognee import SearchType

    st = getattr(SearchType, query_type.upper(), SearchType.GRAPH_COMPLETION)
    kwargs = {"query_text": text, "query_type": st, "top_k": top_k}
    if domain:
        # Restrict retrieval to one category domain (our node_set tags).
        kwargs["node_name"] = [domain]
    return await cognee.search(**kwargs)


def build_graph(settings: Settings, channel: str, *, limit: int | None = None, reset: bool = False):
    import asyncio

    _require_cognee()
    asyncio.run(_build(settings, channel, limit, reset))


def query_graph(
    settings: Settings,
    text: str,
    *,
    query_type: str = "GRAPH_COMPLETION",
    top_k: int = 10,
    domain: str | None = None,
):
    import asyncio

    _require_cognee()
    return asyncio.run(_query(settings, text, query_type, top_k, domain))
