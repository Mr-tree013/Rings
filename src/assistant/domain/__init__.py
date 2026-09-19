"""Domain layer: pure models and rules.

Hard constraints (see AGENTS.md and ADR-0001): this layer must not import adapters,
databases, web frameworks or model SDKs, and must not perform I/O. Currently it holds
`InboundEvent` with its state machine (ADR-0002) and the project's vocabulary errors;
the remaining vocabulary is frozen in the system design spec.
"""
