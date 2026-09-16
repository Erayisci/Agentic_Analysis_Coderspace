"""The only place in the codebase that talks to a language model.

The brief forbids third-party LLM APIs: models come from the Kloudeks platform
and nothing else. Keeping every call behind one client makes that checkable by
grep, and it means swapping the model -- or the whole provider -- touches one
file rather than every tool.
"""
from .client import KloudeksClient, LLMError

__all__ = ["KloudeksClient", "LLMError"]
