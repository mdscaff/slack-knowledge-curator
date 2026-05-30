"""Cognee graph stage — loads classified items via the pgGraph adapter.

See PRD.md §6.5 and docs/cognee-postgres-vs-pggraph.md.
"""

from .cognee_loader import build_graph, query_graph

__all__ = ["build_graph", "query_graph"]
