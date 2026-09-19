"""Ports: interfaces the application depends on.

Adapters implement these. `ModelPort` is the boundary through which any LLM is reached
(ADR-0005); no SDK type may leak past this layer. Currently defined: `Clock` and
`EventRepository` (ADR-0002).
"""
