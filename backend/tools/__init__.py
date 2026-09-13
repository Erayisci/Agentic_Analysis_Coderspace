"""Analytical tools the agent calls: pure functions over lakehouse series.

Every function takes plain Python / pandas inputs and returns a JSON-serialisable
dict, so it can be registered as an LLM tool without an adapter.
"""