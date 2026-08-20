"""
guardrails — the three non-negotiables from CLAUDE.md: allowlist validation against the loaded
file's real schema, a call cap that raises, and an append-only audit log.
executor.py wires them into the single door every tool call must pass through.
"""
