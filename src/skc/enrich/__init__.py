"""Link enrichment stage (X.com oEmbed + generic page titles). See PRD.md §6.2."""

from .links import enrich_channel

__all__ = ["enrich_channel"]
