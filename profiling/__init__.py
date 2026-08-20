"""
profiling — the agent's "first look" at an unseen CSV.
Exists so that every downstream component (toolkit, guardrails, agent) reasons about one
shared, JSON-serialisable description of the file rather than re-reading the DataFrame.
"""
