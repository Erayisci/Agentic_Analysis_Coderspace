"""A minimal OpenAI-compatible client for the Kloudeks (MIA) endpoint.

`httpx` rather than an SDK: the surface used here is three POST bodies, and a
vendor SDK in the dependency tree is exactly what the brief rules out. Nothing
in `tools/` or `agent/` may import this module's transport -- they take a
`KloudeksClient` instance, so a test can substitute a fake without a network.

Three behaviours are deliberate, and each was measured against the live
endpoint rather than assumed:

**Guided decoding.** The server supports `response_format={"type":"json_schema"}`,
which constrains generation to the schema token by token. Plan validity stops
being something a prompt has to beg for and becomes a property of the
transport: a malformed plan is not possible. `structured()` uses it, and falls
back to `json_object` mode, then to bare prompting, if a deployment ever
withdraws support.

**Thinking is opt-in.** Qwen3 reasons before answering. Measured here: 63
completion tokens for a trivial prompt with thinking, 2 without. Schema-filling
gains nothing from it, so `think=False` is the default and callers that want
deliberation (narrative composition) ask for it.

**One repair round.** Guided decoding guarantees the shape, never the meaning:
a plan can be valid JSON and still name a series that does not exist. On a
pydantic validation failure the client re-asks once, quoting the error, then
gives up -- an unbounded repair loop is how an agent burns a demo slot.
"""
import time
from typing import Any, Dict, List, Optional, Type, TypeVar

import httpx
from pydantic import BaseModel, ValidationError

from ..core.config import (
    KLOUDEKS_BASE_URL,
    KLOUDEKS_CHAT_MODEL,
    KLOUDEKS_EMBEDDING_MODEL,
    KLOUDEKS_TIMEOUT_SECONDS,
    kloudeks_api_key,
)

Model = TypeVar("Model", bound=BaseModel)


class LLMError(RuntimeError):
    """Any failure reaching or parsing a model response.

    Tools raise this instead of leaking httpx or json exceptions, so the agent
    can degrade gracefully: on demo day a failed call must cost one step, not
    the turn.
    """


class Usage(BaseModel):
    prompt_tokens: int = 0
    completion_tokens: int = 0
    reasoning_tokens: int = 0
    latency_seconds: float = 0.0
    calls: int = 0

    def add(self, other: "Usage") -> "Usage":
        return Usage(
            prompt_tokens=self.prompt_tokens + other.prompt_tokens,
            completion_tokens=self.completion_tokens + other.completion_tokens,
            reasoning_tokens=self.reasoning_tokens + other.reasoning_tokens,
            latency_seconds=round(self.latency_seconds + other.latency_seconds, 3),
            calls=self.calls + other.calls,
        )


def _strip_json(text: str) -> str:
    """The outermost JSON object in a response.

    Guided decoding makes this a no-op, but a fallback path may return a fenced
    block or a sentence of preamble, and failing on that would be a silly way
    to lose a turn.
    """
    text = text.strip()
    if text.startswith("```"):
        text = text.split("```")[1] if "```" in text[3:] else text[3:]
        text = text[4:] if text.lower().startswith("json") else text
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        raise LLMError(f"no JSON object in model response: {text[:200]!r}")
    return text[start:end + 1]


class KloudeksClient:
    """Chat, structured output and embeddings against one Kloudeks deployment."""

    def __init__(
        self,
        model: str = KLOUDEKS_CHAT_MODEL,
        base_url: str = KLOUDEKS_BASE_URL,
        api_key: Optional[str] = None,
        timeout: float = KLOUDEKS_TIMEOUT_SECONDS,
        think: bool = False,
        temperature: float = 0.0,
    ):
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.think = think
        self.temperature = temperature
        self.usage = Usage()
        self._api_key = api_key or kloudeks_api_key()
        self._client = httpx.Client(
            timeout=httpx.Timeout(timeout, connect=15.0),
            headers={"Authorization": f"Bearer {self._api_key}",
                     "Content-Type": "application/json"},
        )
        # Set once the server rejects a mode, so the whole session stops paying
        # a round trip to rediscover the same unsupported feature.
        self._schema_mode_supported = True

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "KloudeksClient":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # -- transport ---------------------------------------------------------

    def _post(self, path: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        started = time.perf_counter()
        try:
            response = self._client.post(f"{self.base_url}{path}", json=payload)
        except httpx.TimeoutException as exc:
            raise LLMError(f"Kloudeks call timed out after {self.timeout}s") from exc
        except httpx.HTTPError as exc:
            raise LLMError(f"Kloudeks call failed: {type(exc).__name__}") from exc
        elapsed = time.perf_counter() - started

        if response.status_code >= 400:
            # The body can echo the request; never surface it, it carries the key.
            raise LLMError(f"Kloudeks returned HTTP {response.status_code} for {path}")
        try:
            body = response.json()
        except ValueError as exc:
            raise LLMError("Kloudeks returned a non-JSON body") from exc

        usage = body.get("usage") or {}
        self.usage = self.usage.add(Usage(
            prompt_tokens=usage.get("prompt_tokens", 0) or 0,
            completion_tokens=usage.get("completion_tokens", 0) or 0,
            reasoning_tokens=(usage.get("completion_tokens_details") or {}).get("reasoning_tokens", 0) or 0,
            latency_seconds=round(elapsed, 3),
            calls=1,
        ))
        return body

    # Reasoning tokens are billed against max_tokens, so a budget sized for the
    # answer leaves none for the thinking. Measured: the reference demo question
    # consumed all 1400 tokens on reasoning and returned an empty string, which
    # looked like a model failure and was really a budget error.
    REASONING_HEADROOM = 4

    def _chat_payload(self, messages, max_tokens, think, temperature) -> Dict[str, Any]:
        thinking = self.think if think is None else think
        payload: Dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "max_tokens": max_tokens * self.REASONING_HEADROOM if thinking else max_tokens,
            "temperature": self.temperature if temperature is None else temperature,
        }
        if not thinking:
            payload["chat_template_kwargs"] = {"enable_thinking": False}
        return payload

    # -- public API --------------------------------------------------------

    def chat(
        self,
        messages: List[Dict[str, Any]],
        max_tokens: int = 1024,
        think: Optional[bool] = None,
        temperature: Optional[float] = None,
    ) -> str:
        """Plain text completion."""
        body = self._post("/chat/completions",
                          self._chat_payload(messages, max_tokens, think, temperature))
        try:
            return body["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError) as exc:
            raise LLMError("Kloudeks response had no message content") from exc

    def structured(
        self,
        messages: List[Dict[str, Any]],
        schema: Type[Model],
        max_tokens: int = 1500,
        think: Optional[bool] = None,
        temperature: Optional[float] = None,
        repair: bool = True,
    ) -> Model:
        """A validated pydantic object, using the server's guided decoding.

        Raises LLMError if the model cannot produce a valid object in two
        attempts. The caller is expected to have a deterministic fallback --
        see `agent.planner.template_plan`.
        """
        json_schema = schema.model_json_schema()
        payload = self._chat_payload(messages, max_tokens, think, temperature)

        if self._schema_mode_supported:
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": schema.__name__, "schema": json_schema},
            }
        else:
            payload["response_format"] = {"type": "json_object"}

        try:
            body = self._post("/chat/completions", payload)
        except LLMError:
            if not self._schema_mode_supported:
                raise
            # A deployment without guided decoding: degrade once, permanently.
            self._schema_mode_supported = False
            payload["response_format"] = {"type": "json_object"}
            body = self._post("/chat/completions", payload)

        raw = body["choices"][0]["message"]["content"] or ""
        try:
            return schema.model_validate_json(_strip_json(raw))
        except (ValidationError, LLMError) as exc:
            first_error = exc
            if not repair:
                raise LLMError(f"{schema.__name__} validation failed: {exc}") from exc

        # One repair round: quote the error, ask again, then stop.
        repair_messages = messages + [
            {"role": "assistant", "content": raw[:2000]},
            {"role": "user", "content":
                f"That did not validate against the required schema:\n{first_error}\n"
                "Return ONLY a corrected JSON object. Do not explain."},
        ]
        repair_payload = self._chat_payload(repair_messages, max_tokens, think, temperature)
        repair_payload["response_format"] = payload["response_format"]
        body = self._post("/chat/completions", repair_payload)
        raw = body["choices"][0]["message"]["content"] or ""
        try:
            return schema.model_validate_json(_strip_json(raw))
        except (ValidationError, LLMError) as exc:
            raise LLMError(f"{schema.__name__} invalid after one repair attempt: {exc}") from exc

    def embed(self, texts: List[str], model: str = KLOUDEKS_EMBEDDING_MODEL) -> List[List[float]]:
        """Embedding vectors, in input order."""
        body = self._post("/embeddings",
                          {"model": model, "input": texts, "encoding_format": "float"})
        try:
            ordered = sorted(body["data"], key=lambda row: row.get("index", 0))
            return [row["embedding"] for row in ordered]
        except (KeyError, TypeError) as exc:
            raise LLMError("Kloudeks embeddings response had no data") from exc

    def health(self) -> bool:
        """True when the endpoint answers; used by /health and the eval harness."""
        try:
            self._post("/chat/completions", {
                "model": self.model, "messages": [{"role": "user", "content": "ping"}],
                "max_tokens": 4, "temperature": 0,
                "chat_template_kwargs": {"enable_thinking": False},
            })
            return True
        except LLMError:
            return False
