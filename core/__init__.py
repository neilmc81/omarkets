"""Omarkets Python Market Core.

The core is deterministic and is the single source of truth for market facts.
It performs ingestion, normalization, storage, analytics, and state, and it
exposes that state over a localhost API. It does not call LLMs.
"""

__version__ = "0.1.0"

__all__ = ["__version__"]
