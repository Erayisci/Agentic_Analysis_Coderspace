"""The agent: a short deterministic pipeline, not a free-running loop.

    question -> router -> planner -> executor -> verifier -> composer -> answer

The model appears at exactly three points -- classifying intent, emitting a
typed plan, and writing prose over numbers it did not compute. Everything
between those points is Python, because open-weight models in the 27B class
are reliable at filling a schema and unreliable at arithmetic, multi-step
tool choreography, and remembering a table across turns.
"""
