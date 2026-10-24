"""External interface adapters for Omarkets.

These modules sit *outside* the deterministic core and the FastAPI app: they
translate a local consumer (currently the Qwen voice assistant) into calls
against the loopback Omarkets HTTP API. They never import storage, analysis or
service internals, and they never compute a market fact.
"""
