"""KKB agentic analytics backend.

Layering, outermost first: `lakehouse` orchestrates, `transform` and `validation`
operate on parsed frames, `parsing` reads what `ingestion` fetched, and both lean
on `domain` declarations and `core` primitives. Dependencies point inward only.
"""
