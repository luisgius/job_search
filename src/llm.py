"""The single seam between this pipeline and an LLM.

Two jobs, both deliberately boring:

  * `LLMClient` — a thin, retrying wrapper over `messages.create`. Every test
    injects `client=`, so nothing here needs a network (or an API key) to be
    exercised, and `anthropic` is imported lazily so the suite runs without it
    installed at all.
  * `extract_json` — models wrap their JSON in prose and code fences no matter
    how firmly the prompt forbids it. Recovering the object is a parsing
    problem with a correct answer, so it is solved with a brace scan that
    understands string literals rather than with a regex that mostly works.

Everything that talks to the API raises `LLMError` and nothing else: callers
(scoring, tailoring) turn that into a degraded result instead of a dead run.
"""

from __future__ import annotations

import json
import re
import threading
import time
from collections.abc import Mapping
from typing import Any, Callable, Iterable, Iterator

from .util import get_logger

logger = get_logger(__name__)

#: Retries *after* the first attempt. 2 => at most 3 calls.
DEFAULT_MAX_RETRIES = 2
#: Backoff base in seconds; attempt N waits `BASE * 2**(N-1)`.
RETRY_BASE_DELAY = 0.5

#: HTTP statuses that are worth trying again. Everything else (400, 401, 403,
#: 404, 422 ...) is a bug in the request and will fail identically forever.
RETRYABLE_STATUS: frozenset[int] = frozenset({408, 429, 500, 502, 503, 529})

# The SDK's transient errors, matched by class name so we never have to import
# `anthropic` just to catch them. Lowercased substrings.
_TRANSIENT_NAME_TOKENS: tuple[str, ...] = (
    "ratelimit", "apiconnection", "apitimeout", "internalserver", "overloaded",
)

_FENCE_RE = re.compile(r"```([A-Za-z0-9_+-]*)[ \t]*\r?\n?(.*?)(?:```|\Z)", re.DOTALL)

#: Ceiling on how many balanced `{...}` spans we will try to parse out of one
#: response. Prose full of braces should not turn recovery into O(n^2) work.
_MAX_CANDIDATES = 40


#: Providers `LLMClient` can talk to.
PROVIDERS: tuple[str, ...] = ("anthropic", "openrouter")

DEFAULT_PROVIDER = "openrouter"

#: OpenRouter speaks the OpenAI chat-completions dialect, so this base URL also
#: works for any other OpenAI-compatible gateway (LiteLLM, vLLM, Together...)
#: via `llm.base_url`.
OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"

#: OpenRouter uses these for attribution on its public leaderboards. Optional,
#: and deliberately generic — nothing here identifies the user.
OPENROUTER_REFERER = "https://github.com/job-hunter"
OPENROUTER_TITLE = "job-hunter"

#: Where each provider's key comes from. Config path -> env var.
PROVIDER_KEYS: dict[str, tuple[str, str]] = {
    "anthropic": ("keys.anthropic", "ANTHROPIC_API_KEY"),
    "openrouter": ("keys.openrouter", "OPENROUTER_API_KEY"),
}


#: `llm.structured_output` values. "auto" asks for grammar-constrained JSON
#: only where it is a documented first-party feature (the Anthropic SDK);
#: the OpenAI dialect's `response_format` passthrough varies per gateway and
#: per model — many `:free` ids reject it — so auto leaves that path on
#: prompt+extract_json until the user opts a model in with "native".
STRUCTURED_MODES: tuple[str, ...] = ("auto", "native", "prompt")


def _native_schema_wanted(mode: str, provider: str) -> bool:
    mode = str(mode or "auto").strip().lower()
    if mode == "native":
        return True
    if mode == "prompt":
        return False
    return provider == "anthropic"


def _format_unsupported(exc: BaseException) -> bool:
    """Does this failure look like 'this model/gateway rejects the schema
    parameter' rather than a broken request? Those retry on the prompt path
    — one extra call, bounded, logged — instead of losing the job."""
    if _status_code(exc) != 400:
        return False
    text = str(exc).lower()
    return any(tok in text for tok in
               ("output_config", "response_format", "json_schema", "format",
                "structured", "schema"))


# --------------------------------------------------------------------------
# usage telemetry
# --------------------------------------------------------------------------


class UsageMeter:
    """Token and cost counters for one run.

    Shared by every client the run constructs (scoring's thread pool
    included, hence the lock) and read once by `main` for the digest. A
    failed read of a provider's `usage` block must never break the call
    that carried it, so recording is best-effort by construction: the
    transports only feed it numbers they already extracted.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.reset()

    def reset(self) -> None:
        with self._lock:
            self.calls = 0
            self.input_tokens = 0
            self.output_tokens = 0
            self.cost = 0.0
            self.by_model: dict[str, dict[str, Any]] = {}

    def add(self, *, model: str, provider: str, input_tokens: int = 0,
            output_tokens: int = 0, cost: float = 0.0) -> None:
        with self._lock:
            self.calls += 1
            self.input_tokens += max(0, int(input_tokens))
            self.output_tokens += max(0, int(output_tokens))
            self.cost += max(0.0, float(cost))
            row = self.by_model.setdefault(
                str(model),
                {"provider": str(provider), "calls": 0,
                 "input_tokens": 0, "output_tokens": 0, "cost": 0.0},
            )
            row["calls"] += 1
            row["input_tokens"] += max(0, int(input_tokens))
            row["output_tokens"] += max(0, int(output_tokens))
            row["cost"] += max(0.0, float(cost))

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "calls": self.calls,
                "input_tokens": self.input_tokens,
                "output_tokens": self.output_tokens,
                # OpenRouter reports the charge in the response; providers
                # that do not leave this at 0.0, which the digest prints as
                # "cost unreported", never as "free".
                "cost": round(self.cost, 6),
                "by_model": {k: dict(v) for k, v in self.by_model.items()},
            }


#: The run-wide meter. One process, one run, one bill — `main` resets it at
#: pipeline start and snapshots it into `RunStats` for the digest.
METER = UsageMeter()


def reset_usage() -> None:
    METER.reset()


def usage_snapshot() -> dict[str, Any]:
    return METER.snapshot()


def _usage_number(node: Any, *keys: str) -> int:
    """First integer found under `keys`, object- or dict-shaped, else 0."""
    for key in keys:
        if isinstance(node, Mapping):
            value = node.get(key)
        else:
            value = getattr(node, key, None)
        if isinstance(value, bool):
            continue
        if isinstance(value, (int, float)):
            return int(value)
    return 0


def _usage_cost(usage: Any) -> float:
    """OpenRouter's per-response charge (fractional dollars), else 0.0."""
    value = usage.get("cost") if isinstance(usage, Mapping) \
        else getattr(usage, "cost", None)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0.0
    return max(0.0, float(value))


class LLMError(RuntimeError):
    """Any failure to get a usable answer out of the model.

    `status_code` and `transient` are optional and exist for the HTTP
    transport, which has no SDK exception classes to classify by. Both default
    to None, so every plain `LLMError("...")` behaves exactly as before.
    """

    def __init__(self, message: Any = "", *, status_code: int | None = None,
                 transient: bool | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.transient = transient


# --------------------------------------------------------------------------
# client
# --------------------------------------------------------------------------


#: Last resort for statuses that only exist in an error's text — which is the
#: case for every OpenRouter failure, since those arrive as an `HttpError`
#: whose message embeds "-> HTTP 429" rather than an attribute.
_TEXT_STATUS_RE = re.compile(r"\bHTTP[: ]+(\d{3})\b")


def _status_code(exc: BaseException) -> int | None:
    """Best-effort HTTP status from an exception, or None."""
    for candidate in (
        getattr(exc, "status_code", None),
        getattr(getattr(exc, "response", None), "status_code", None),
    ):
        if candidate is None or isinstance(candidate, bool):
            continue
        try:
            return int(candidate)
        except (TypeError, ValueError):
            continue
    found = _TEXT_STATUS_RE.search(str(exc))
    return int(found.group(1)) if found else None


def _is_transient(exc: BaseException) -> bool:
    """Should this failure be retried?

    An explicit verdict wins over everything: the HTTP transport already knows
    whether a failure was a status or a dead socket, and it has no SDK
    exception classes to be classified by.

    Otherwise an explicit status wins over the class name: a 400 named
    `InternalServerError` is still a malformed request, and retrying it just
    burns the budget. Only when there is no status do we fall back to the name
    (that is the `APIConnectionError` / socket-level case, which has none).
    """
    verdict = getattr(exc, "transient", None)
    if verdict is not None:
        return bool(verdict)

    status = _status_code(exc)
    if status is not None:
        # Any 5xx is worth one more try: gateways in front of both providers
        # emit 504/520/524, which an explicit allow-list keeps missing.
        return status in RETRYABLE_STATUS or 500 <= status < 600
    name = type(exc).__name__.lower()
    return any(token in name for token in _TRANSIENT_NAME_TOKENS)


def _block_text(block: Any) -> str:
    """Text of one content block, for both object- and dict-shaped blocks."""
    if isinstance(block, Mapping):
        if str(block.get("type", "text")) != "text":
            return ""
        return str(block.get("text") or "")
    if str(getattr(block, "type", "text")) != "text":
        return ""
    return str(getattr(block, "text", "") or "")


def _openrouter_text(payload: Any) -> str:
    """Pull the assistant text out of an OpenAI-style chat completion.

    Handles the three shapes seen in the wild: `content` as a plain string,
    `content` as a list of `{"type": "text", "text": ...}` parts (some models
    on OpenRouter answer this way), and a provider-level `error` object
    returned with HTTP 200 — which is the one that would otherwise be read as
    an empty answer and silently scored 0.
    """
    if not isinstance(payload, Mapping):
        raise LLMError(f"openrouter returned {type(payload).__name__}, not an object")

    error = payload.get("error")
    if isinstance(error, Mapping) and error:
        message = error.get("message") or error
        raise LLMError(f"openrouter error: {message}")

    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        raise LLMError(f"openrouter returned no choices: {str(payload)[:200]}")

    message = choices[0].get("message") if isinstance(choices[0], Mapping) else None
    if not isinstance(message, Mapping):
        raise LLMError("openrouter choice carried no message")

    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for part in content:
            if isinstance(part, Mapping):
                parts.append(str(part.get("text") or ""))
            elif isinstance(part, str):
                parts.append(part)
        return "".join(parts)
    # Reasoning models sometimes answer with only `reasoning` populated.
    return str(message.get("reasoning") or "")


def _message_text(message: Any) -> str:
    """Concatenate every text block of a `messages.create` response.

    Blocks are contiguous pieces of one stream, so they are joined with no
    separator; non-text blocks (tool_use, thinking) are dropped.
    """
    if isinstance(message, Mapping):
        content = message.get("content")
    else:
        content = getattr(message, "content", None)
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    return "".join(_block_text(block) for block in content)


class LLMClient:
    """Model wrapper with retries, injectable for tests.

    Two providers, one surface. `complete()` and `complete_json()` behave
    identically whichever is in use, so `scoring` and `tailor` never learn
    which one they are talking to.

      anthropic   the SDK. Seam: `client=` — pass anything exposing
                  `.messages.create(**kwargs)` and no SDK import happens.
      openrouter  the OpenAI chat-completions dialect over plain HTTP, which
                  also covers any OpenAI-compatible gateway via `base_url=`.
                  Seam: `session=` — the same `FakeSession` the sources use.

    Model names are passed through untouched. On OpenRouter they are
    vendor-qualified (`anthropic/claude-sonnet-5`, `openai/gpt-5`); mapping
    them automatically would silently send work to a model you did not choose.
    """

    def __init__(
        self,
        api_key: str,
        *,
        client: Any = None,
        provider: str | None = None,
        base_url: str | None = None,
        session: Any = None,
        max_retries: int = DEFAULT_MAX_RETRIES,
        sleep: Callable[[float], None] | None = None,
        headers: Mapping[str, str] | None = None,
        meter: UsageMeter | None = None,
    ) -> None:
        # Each provider speaks through exactly one seam: `client=` is the
        # Anthropic SDK object, `session=` is the openrouter HTTP session.
        # An omitted provider is therefore inferable from which seam was
        # injected — and a *contradictory* pair is an error, not a shrug.
        # This is not pedantry: when the shipped default flipped to
        # openrouter, every test that wrapped a FakeAnthropic without naming
        # its provider kept constructing happily while `complete()` routed
        # around the fake and POSTed to the real internet, 54 times. A seam
        # that can silently discard its injection is how an offline suite
        # stops being one.
        if provider is None:
            resolved = "anthropic" if client is not None else DEFAULT_PROVIDER
        else:
            resolved = str(provider or DEFAULT_PROVIDER).strip().lower()
        self.provider = resolved
        if self.provider not in PROVIDERS:
            raise LLMError(
                f"unknown llm.provider {provider!r} — expected one of "
                f"{', '.join(PROVIDERS)}"
            )
        if self.provider == "openrouter" and client is not None:
            raise LLMError(
                "an SDK client= was injected but the provider is openrouter, "
                "which speaks over session= — the injected client would be "
                "silently ignored and every call would hit the real network"
            )
        if self.provider == "anthropic" and session is not None:
            raise LLMError(
                "a session= was injected but the provider is anthropic, "
                "which speaks through the SDK client= — the injected session "
                "would be silently ignored"
            )
        self.api_key = str(api_key or "")
        self.max_retries = max(0, int(max_retries))
        self._sleep = sleep or time.sleep
        self._injected = client
        self._session = session
        self._real: Any = None
        self._extra_headers = dict(headers or {})
        self._meter = meter if meter is not None else METER
        self.base_url = str(
            base_url or (OPENROUTER_BASE_URL if self.provider == "openrouter" else "")
        ).rstrip("/")

        # A key is only dispensable when a seam replaces the transport — or
        # when the OpenAI dialect points at a non-OpenRouter gateway: a local
        # Ollama/vLLM endpoint has no key to give, and demanding one would
        # block exactly the fallback entry that works with the network down.
        keyless_gateway = (
            self.provider == "openrouter"
            and self.base_url != OPENROUTER_BASE_URL
        )
        has_seam = client is not None or (
            self.provider == "openrouter" and session is not None
        )
        if not has_seam and not keyless_gateway and not self.api_key:
            # Fail here rather than three stages later with a stack trace from
            # inside the SDK. `config.validate` catches this first in practice.
            path, env = PROVIDER_KEYS[self.provider]
            raise LLMError(
                f"No {self.provider} API key — set {path} in config.yaml or "
                f"export {env}"
            )

    # -- plumbing ---------------------------------------------------------

    @property
    def client(self) -> Any:
        """The underlying SDK client, constructed on first use."""
        if self._injected is not None:
            return self._injected
        if self._real is None:
            try:
                import anthropic  # local import: keeps stdlib-only tests importable
            except ImportError as exc:  # pragma: no cover - depends on env
                raise LLMError(
                    "The `anthropic` package is not installed — "
                    "pip install -r requirements.txt"
                ) from exc
            self._real = anthropic.Anthropic(api_key=self.api_key)
        return self._real

    # -- transports -------------------------------------------------------

    def _call_anthropic(self, *, model: str, system: str, prompt: str,
                        max_tokens: int, temperature: float,
                        schema_native: Mapping[str, Any] | None = None) -> str:
        kwargs: dict[str, Any] = dict(
            model=model,
            max_tokens=max_tokens,
            temperature=temperature,
            system=system,
            messages=[{"role": "user", "content": prompt}],
        )
        if schema_native:
            # Grammar-constrained decoding: the response's text IS the JSON.
            # `output_config.format` is the current parameter; the older
            # `output_format` is deprecated.
            kwargs["output_config"] = {
                "format": {"type": "json_schema", "schema": dict(schema_native)}
            }
        response = self.client.messages.create(**kwargs)
        self._record_usage(model, getattr(response, "usage", None)
                           if not isinstance(response, Mapping)
                           else response.get("usage"))
        return _message_text(response)

    def _record_usage(self, model: str, usage: Any) -> None:
        """Feed the meter from a provider `usage` block. Never raises."""
        try:
            self._meter.add(
                model=model,
                provider=self.provider,
                # The two dialects spell the same numbers differently.
                input_tokens=_usage_number(usage, "input_tokens", "prompt_tokens"),
                output_tokens=_usage_number(usage, "output_tokens",
                                            "completion_tokens"),
                cost=_usage_cost(usage),
            )
        except Exception as exc:  # telemetry must never cost a completion
            logger.debug("usage recording failed for %s: %s", model, exc)

    def _call_openrouter(self, *, model: str, system: str, prompt: str,
                         max_tokens: int, temperature: float,
                         schema_native: Mapping[str, Any] | None = None) -> str:
        """One OpenAI-style chat completion over plain HTTP.

        Deliberately not the `openai` SDK: this is one POST, and going through
        `util.http_post_json` keeps the retry/User-Agent behaviour identical to
        every other network call in the project and makes `session=` the same
        seam the sources already use.

        The system prompt becomes a `system` role message, which is how the
        chat-completions dialect expresses what Anthropic passes separately.
        """
        from .util import HttpError, http_post_json

        headers = {
            "Content-Type": "application/json",
            # Attribution headers OpenRouter asks for; nothing user-identifying.
            "HTTP-Referer": OPENROUTER_REFERER,
            "X-Title": OPENROUTER_TITLE,
        }
        if self.api_key:  # a local gateway (Ollama, vLLM) has no key to send
            headers["Authorization"] = f"Bearer {self.api_key}"
        headers.update(self._extra_headers)

        payload = {
            "model": model,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "messages": (
                ([{"role": "system", "content": system}] if system else [])
                + [{"role": "user", "content": prompt}]
            ),
        }
        if schema_native:
            # The OpenAI dialect's grammar constraint. Only sent when the
            # capability decision (`_native_schema_wanted`) said this
            # (provider, model) supports it; gateways that do not answer 400
            # and `complete_json` retries on the prompt path.
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": "response", "strict": True,
                                "schema": dict(schema_native)},
            }
        if self.base_url == OPENROUTER_BASE_URL:
            # OpenRouter-only extension: the response then carries the charge
            # in `usage.cost` — telemetry without scraping a dashboard. Not
            # sent to other gateways, whose stricter parsers may reject it.
            payload["usage"] = {"include": True}
        try:
            data = http_post_json(
                f"{self.base_url}/chat/completions",
                json=payload,
                headers=headers,
                session=self._session,
                retries=1,           # LLMClient owns the retry loop, not util
            )
        except HttpError as exc:
            # Translate into the vocabulary the retry loop understands. A
            # status means the server answered and we can judge it; no status
            # means the socket died, which is the same case the Anthropic SDK
            # reports as APIConnectionError and retries.
            status = _status_code(exc)
            if status is None:
                raise LLMError(f"openrouter request failed: {exc}",
                               transient=True) from exc
            raise LLMError(str(exc), status_code=status) from exc
        text = _openrouter_text(data)
        self._record_usage(model, data.get("usage")
                           if isinstance(data, Mapping) else None)
        return text

    # -- calls ------------------------------------------------------------

    def complete(
        self,
        *,
        model: str,
        system: str,
        prompt: str,
        max_tokens: int,
        temperature: float = 0.0,
        sleep: Callable[[float], None] | None = None,
        schema_native: Mapping[str, Any] | None = None,
    ) -> str:
        """One completion, returned as plain text.

        Retries transient failures up to `max_retries` times with exponential
        backoff; auth and 4xx errors are re-raised immediately as `LLMError`
        because they will never succeed. Never raises anything but `LLMError`.

        `schema_native` asks the provider for grammar-constrained JSON
        (`complete_json` decides when — callers wanting text never set it).
        """
        sleeper = sleep or self._sleep
        attempts = self.max_retries + 1
        last: BaseException | None = None

        for attempt in range(attempts):
            if attempt:
                sleeper(RETRY_BASE_DELAY * (2 ** (attempt - 1)))
            try:
                call = (self._call_openrouter if self.provider == "openrouter"
                        else self._call_anthropic)
                text = call(model=model, system=system, prompt=prompt,
                            max_tokens=max_tokens, temperature=temperature,
                            schema_native=schema_native)
            except LLMError as exc:
                # An HTTP-layer LLMError can still be transient (429/5xx), so
                # it is classified like any other failure rather than re-raised
                # blind — otherwise OpenRouter would never retry at all.
                last = exc
                if not _is_transient(exc):
                    raise
                logger.warning(
                    "transient LLM error from %s (attempt %d/%d): %s",
                    model, attempt + 1, attempts, exc,
                )
                continue
            except Exception as exc:
                last = exc
                if not _is_transient(exc):
                    # The status survives the wrapping: `complete_json` reads
                    # it to tell "this gateway rejects the schema parameter"
                    # apart from a genuinely broken request.
                    raise LLMError(f"{model} call failed: {exc}",
                                   status_code=_status_code(exc)) from exc
                logger.warning(
                    "transient LLM error from %s (attempt %d/%d): %s",
                    model, attempt + 1, attempts, exc,
                )
                continue

            if not text.strip():
                logger.warning("%s returned no text content", model)
            return text

        raise LLMError(f"{model} call failed after {attempts} attempts: {last}")

    def complete_json(
        self,
        *,
        model: str,
        system: str,
        prompt: str,
        max_tokens: int,
        temperature: float = 0.0,
        schema_hint: str = "",
        require_keys: Iterable[str] | None = None,
        forbid_verbatim: str | None = None,
        schema: Mapping[str, Any] | None = None,
        structured: str = "auto",
        sleep: Callable[[float], None] | None = None,
    ) -> dict[str, Any]:
        """`complete()` plus `extract_json()`.

        `schema_hint` is appended to the prompt when given — a literal example
        of the wanted object buys more compliance than any amount of prose.

        `schema` + `structured` choose the transport for the object itself.
        When the (mode, provider) pair supports it, the schema is enforced by
        grammar-constrained decoding: the reply IS the object, `require_keys`
        is absorbed by the schema, and `forbid_verbatim` changes role from
        selector to validator — a constrained reply that reproduces the
        posting verbatim is refused outright, because with exactly one object
        there is no later candidate to prefer. Everywhere else (including a
        gateway that 400s the schema parameter — retried once, prompt-path)
        the existing prompt + `extract_json` route is unchanged. Never delete
        that route: it is the fallback and the second validation layer.
        """
        if schema and _native_schema_wanted(structured, self.provider):
            try:
                reply = self.complete(
                    model=model, system=system, prompt=prompt,
                    max_tokens=max_tokens, temperature=temperature,
                    sleep=sleep, schema_native=schema,
                )
            except LLMError as exc:
                if not _format_unsupported(exc):
                    raise
                logger.warning(
                    "%s rejected native structured output (%s) — retrying on "
                    "the prompt path", model, exc,
                )
            else:
                candidate = _try_load(reply)
                if candidate is None:
                    # A "constrained" reply that is not JSON is a gateway
                    # that ignored the parameter; the recovery scan still
                    # applies and costs no extra call.
                    return extract_json(reply, require_keys=require_keys,
                                        forbid_verbatim=forbid_verbatim)
                untrusted = str(forbid_verbatim or "")
                if untrusted and _looks_lifted_from(candidate, untrusted):
                    raise LLMError(
                        "the structured reply reproduces the job posting "
                        "verbatim — refusing planted text as an answer"
                    )
                return candidate

        text = prompt
        if schema_hint:
            text = (
                f"{prompt}\n\nRespond with a single JSON object and nothing "
                f"else — no prose, no code fences. Expected shape:\n{schema_hint}"
            )
        return extract_json(
            self.complete(
                model=model,
                system=system,
                prompt=text,
                max_tokens=max_tokens,
                temperature=temperature,
                sleep=sleep,
            ),
            require_keys=require_keys,
            forbid_verbatim=forbid_verbatim,
        )


# --------------------------------------------------------------------------
# JSON recovery
# --------------------------------------------------------------------------


def _match_brace(text: str, start: int) -> int | None:
    """Index of the `}` closing the `{` at `start`, or None if unbalanced.

    String-aware: braces and quotes inside a JSON string literal (and anything
    behind a backslash) do not move the depth counter. This is the whole
    reason `extract_json` is not a regex — `{"a": "}"}` is valid JSON and a
    greedy or lazy regex gets one of those two cases wrong.
    """
    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return index
    return None


def _balanced_objects(text: str) -> Iterator[str]:
    """Yield balanced `{...}` spans, outermost first, in document order."""
    yielded = 0
    for index, char in enumerate(text):
        if char != "{":
            continue
        end = _match_brace(text, index)
        if end is None:
            continue
        yield text[index:end + 1]
        yielded += 1
        if yielded >= _MAX_CANDIDATES:
            return


def _strip_trailing_commas(text: str) -> str:
    """Drop `,` that sits directly before a `}` or `]` (outside strings).

    The single repair pass we allow ourselves: trailing commas are by far the
    most common way a model produces almost-JSON, and removing them cannot
    change the meaning of anything that was already valid.
    """
    out: list[str] = []
    in_string = False
    escaped = False
    for index, char in enumerate(text):
        if in_string:
            out.append(char)
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
            out.append(char)
            continue
        if char == ",":
            rest = text[index + 1:]
            stripped = rest.lstrip()
            if stripped[:1] in ("}", "]"):
                continue  # drop this comma
        out.append(char)
    return "".join(out)


def _try_load(chunk: str) -> dict[str, Any] | None:
    """Parse `chunk` as a JSON object, with one trailing-comma repair pass."""
    chunk = chunk.strip()
    if not chunk:
        return None
    for candidate in (chunk, _strip_trailing_commas(chunk)):
        try:
            value = json.loads(candidate)
        except (ValueError, TypeError):
            continue
        if isinstance(value, dict):
            return value
        return None  # valid JSON, wrong shape — a repair will not fix that
    return None


def _fenced_blocks(text: str) -> list[str]:
    """Bodies of ``` fences, json-tagged ones first.

    An unterminated fence (the model ran out of tokens) still yields its body,
    which is usually enough to recover the object.
    """
    tagged: list[str] = []
    untagged: list[str] = []
    for lang, body in _FENCE_RE.findall(text):
        (tagged if lang.lower() in {"json", "json5", "jsonc"} else untagged).append(body)
    return tagged + untagged


def extract_json(
    text: str,
    *,
    require_keys: Iterable[str] | None = None,
    forbid_verbatim: str | None = None,
) -> dict[str, Any]:
    """Recover a JSON object from a model reply.

    `require_keys` is the defence against a planted object. A job ad can carry
    a complete, valid-looking score object, and a model that quotes the
    instruction it is refusing — "the posting asks me to return {...}, but my
    assessment is {...}" — hands this function two parseable objects with the
    attacker's first.

    Matching is on the FULL key set, not any of them: a plant usually carries
    `score` and `verdict`, so "has one of the keys" does not discriminate.

    That alone is not enough — a real ad can plant a complete five-key object,
    and then BOTH candidates match. Two more things decide it:

    * `forbid_verbatim` is the untrusted text the reply is about (the job
      description). Any candidate whose own keys and values all appear inside
      it is text lifted from the ad, not an answer, and is skipped. This is
      the precise defence and it kills the verbatim-quote attack outright.
    * failing that, the LAST full-key match wins rather than the first. A model
      that quotes the instruction it is refusing puts the quote mid-sentence
      and its own answer at the end.

    When nothing carries the full set we do NOT go looking for a closer
    match: preferring an object that has *some* of the keys is exactly what
    lets a partial plant win. The first parseable object is returned as-is, so
    an envelope like `{"result": {...}}` reaches `parse_score`, which records
    an honest scoring error and still shows the job for a human to judge.
    Guessing at a number would be worse than admitting we could not read one.
    """
    raw = text or ""
    if not str(raw).strip():
        raise LLMError("model returned nothing")

    wanted = {str(k) for k in (require_keys or ())}
    untrusted = str(forbid_verbatim or "")
    parsed_any: dict[str, Any] | None = None
    matches: list[dict[str, Any]] = []
    saw_lifted = False

    for chunk in _candidate_chunks(raw):
        candidate = _try_load(chunk)
        if candidate is None:
            continue
        # Lifted-ness disqualifies a candidate from EVERY role, the unjudged
        # fallback included: a reply consisting solely of the planted object
        # must error out, not ride through as "the first parseable object".
        if untrusted and _looks_lifted_from(candidate, untrusted):
            logger.warning(
                "skipping a JSON object that appears verbatim in the job "
                "posting — it is planted text, not an answer"
            )
            saw_lifted = True
            continue
        if wanted and wanted.issubset(candidate):
            matches.append(candidate)
        if parsed_any is None:
            parsed_any = candidate

    if matches:
        return matches[-1]

    if parsed_any is not None:
        if wanted:
            logger.warning(
                "no object in the model reply carried the full expected key set "
                "(%s) — passing the first one through unjudged",
                ", ".join(sorted(wanted)),
            )
        return parsed_any

    if saw_lifted:
        raise LLMError(
            "every JSON object in the model reply appears verbatim in the "
            "posting — refusing planted text as an answer"
        )

    preview = str(raw).strip().replace("\n", " ")[:200]
    raise LLMError(f"no JSON object found in model response: {preview!r}")


def _looks_lifted_from(candidate: Mapping[str, Any], untrusted: str) -> bool:
    """Does this object appear, key and value, inside the untrusted text?

    Compared on normalised fragments rather than on the exact serialisation,
    because a model re-emits JSON with its own spacing. A real answer is about
    the posting and does not reproduce it.
    """
    haystack = " ".join(str(untrusted).split()).lower()
    if not haystack:
        return False
    fragments = 0
    for key, value in candidate.items():
        if isinstance(value, (dict, list)) and not value:
            continue                       # empty list/dict is not evidence
        fragment = f'"{key}"'
        if fragment.lower() not in haystack:
            return False
        if isinstance(value, (str, int, float)) and not isinstance(value, bool):
            if str(value).lower() not in haystack:
                return False
        fragments += 1
    return fragments >= 2


def _candidate_chunks(raw: str) -> Iterator[str]:
    """Every span of the reply that might be the object, best guess first."""
    for block in _fenced_blocks(raw):
        yield block
    yield raw
    for candidate in _balanced_objects(raw):
        yield candidate


# --------------------------------------------------------------------------
# building a client from config
# --------------------------------------------------------------------------


def _cfg(config: Any, dotted: str, default: Any = None) -> Any:
    """Read a dotted key from a `Config` *or* a bare nested dict."""
    if config is None:
        return default
    if isinstance(config, Mapping):
        node: Any = config
        for part in dotted.split("."):
            if not isinstance(node, Mapping) or part not in node:
                return default
            node = node[part]
        return node
    getter = getattr(config, "get", None)
    return getter(dotted, default) if callable(getter) else default


def provider_of(config: Any) -> str:
    """The configured provider name, normalised and validated."""
    name = str(_cfg(config, "llm.provider", DEFAULT_PROVIDER) or DEFAULT_PROVIDER)
    name = name.strip().lower()
    if name not in PROVIDERS:
        raise LLMError(
            f"unknown llm.provider {name!r} — expected one of {', '.join(PROVIDERS)}"
        )
    return name


def api_key_for(config: Any, provider: str | None = None) -> str:
    """The key belonging to the configured provider.

    Reading `keys.anthropic` while `llm.provider` is `openrouter` is how a
    switched provider silently keeps using the old credential, so the lookup
    is always driven by the provider rather than hard-coded.
    """
    name = provider or provider_of(config)
    path, _env = PROVIDER_KEYS[name]
    return str(_cfg(config, path, "") or "")


def client_from_config(config: Any, **kwargs: Any) -> "LLMClient":
    """Build the `LLMClient` the config asks for.

    The single place that knows how provider, key and base URL fit together —
    `scoring` and `tailor` just call this and stay provider-agnostic.
    """
    provider = provider_of(config)
    return LLMClient(
        api_key_for(config, provider),
        provider=provider,
        base_url=str(_cfg(config, "llm.base_url", "") or "") or None,
        **kwargs,
    )


def structured_mode(config: Any) -> str:
    """The configured `llm.structured_output`, normalised; unknown -> auto."""
    mode = str(_cfg(config, "llm.structured_output", "auto") or "auto")
    mode = mode.strip().lower()
    return mode if mode in STRUCTURED_MODES else "auto"


# --------------------------------------------------------------------------
# the model chain: fallbacks per role
# --------------------------------------------------------------------------


def model_entries(config: Any, role: str) -> list[dict[str, Any]]:
    """The ordered chain for one role: `<role>.model` first, then each
    `<role>.fallback_models` entry.

    A fallback is a model-id string (inherits the global provider and
    base_url) or a mapping with `model` plus optional `provider`/`base_url` —
    the mapping form is how a local gateway joins the chain:

        fallback_models:
          - mistralai/mistral-small-3.1-24b-instruct:free
          - {model: "qwen3.8:27b", base_url: "http://localhost:11434/v1"}
    """
    entries: list[dict[str, Any]] = []
    primary = str(_cfg(config, f"{role}.model", "") or "").strip()
    if primary:
        entries.append({"model": primary, "provider": None, "base_url": None})
    raw = _cfg(config, f"{role}.fallback_models", []) or []
    for item in raw if isinstance(raw, list) else []:
        if isinstance(item, Mapping):
            model = str(item.get("model") or "").strip()
            provider = str(item.get("provider") or "").strip().lower() or None
            base_url = str(item.get("base_url") or "").strip() or None
        else:
            model = str(item or "").strip()
            provider = base_url = None
        entry = {"model": model, "provider": provider, "base_url": base_url}
        # A duplicate buys a second identical failure, nothing else.
        if model and entry not in entries:
            entries.append(entry)
    return entries


class ModelChain:
    """`complete()`/`complete_json()` over an ordered list of models.

    Its one job is surviving a dead entry — a rotated `:free` id, a spent
    daily quota, an outage — so it advances on ANY `LLMError`, after the
    inner client's own retry loop has already dealt with transients. With
    chains this short, finer failure classification buys nothing but bugs.
    The `model=` argument callers pass is superseded by each entry's own
    model; `last_model` reports which entry finally answered so the score
    can be attributed honestly (advisory under scoring concurrency — it is
    the last completion's model, not a per-call receipt).
    """

    def __init__(self, entries: list[tuple[Callable[[], Any], str]]) -> None:
        self._entries = list(entries)
        self.last_model: str | None = None

    def _walk(self, method: str, kwargs: dict[str, Any]) -> Any:
        last: BaseException | None = None
        for build, model in self._entries:
            try:
                target = getattr(build(), method)
            except LLMError as exc:
                logger.warning("model chain: %s is unusable (%s) — trying the "
                               "next entry", model, exc)
                last = exc
                continue
            try:
                result = target(**{**kwargs, "model": model})
            except LLMError as exc:
                logger.warning("model chain: %s failed (%s) — trying the next "
                               "entry", model, exc)
                last = exc
                continue
            self.last_model = model
            return result
        raise last if last is not None else LLMError("the model chain is empty")

    def complete(self, **kwargs: Any) -> str:
        return self._walk("complete", kwargs)

    def complete_json(self, **kwargs: Any) -> dict[str, Any]:
        return self._walk("complete_json", kwargs)


def _client_factory(
    config: Any, entry: Mapping[str, Any], *, injected: Any = None,
    **kwargs: Any
) -> Callable[[], Any]:
    """Lazily build (and cache) the client for one chain entry.

    Lazy on purpose: a fallback whose key is missing must cost nothing until
    the chain actually reaches it, and then cost one cached error rather than
    one per job. Every construction failure surfaces as `LLMError` so the
    chain treats "cannot build" exactly like "cannot answer".
    """
    state: dict[str, Any] = {}

    def build() -> Any:
        if "client" in state:
            return state["client"]
        if "error" in state:
            raise state["error"]
        if injected is not None:
            state["client"] = injected
            return injected
        try:
            provider = entry.get("provider") or provider_of(config)
            client = LLMClient(
                api_key_for(config, provider) if provider in PROVIDER_KEYS
                else "",
                provider=provider,
                base_url=entry.get("base_url")
                or (str(_cfg(config, "llm.base_url", "") or "") or None),
                **kwargs,
            )
        except LLMError as exc:
            state["error"] = exc
            raise
        except Exception as exc:
            error = LLMError(f"chain entry {entry.get('model')!r}: {exc}")
            state["error"] = error
            raise error from exc
        state["client"] = client
        return client

    return build


def chain_from_config(
    config: Any, role: str, *, client: Any = None,
    clients: list[Any] | None = None, **kwargs: Any
) -> Any:
    """What `scoring`/`tailor` should talk to for one role.

    With no fallbacks configured this returns the injected client or a plain
    `LLMClient` — the exact pre-chain behaviour, eager key check included, so
    a missing key still fails once and loudly rather than once per job. With
    fallbacks it returns a `ModelChain`. `clients` (a test seam) overrides
    construction entry-by-entry; a None slot builds normally.

    An injected `ModelChain` is already the resolved thing and passes through
    untouched. `score_jobs`/`tailor_jobs` resolve once and their per-job
    helpers resolve again with the result; wrapping it in a second chain made
    every fallback answer report the primary model's name, retried the
    fallback entries a second time after the inner chain had already failed —
    and, because the wrapper's outer entries were freshly built real clients,
    let a failing injected fake route around its seam to the live network.
    """
    if isinstance(client, ModelChain):
        return client
    entries = model_entries(config, role)
    overrides = list(clients or [])
    if len(entries) <= 1 and not overrides:
        return client if client is not None else client_from_config(config, **kwargs)
    pairs: list[tuple[Callable[[], Any], str]] = []
    for index, entry in enumerate(entries):
        injected = None
        if index < len(overrides) and overrides[index] is not None:
            injected = overrides[index]
        elif index == 0 and client is not None:
            injected = client
        pairs.append(
            (_client_factory(config, entry, injected=injected, **kwargs),
             entry["model"])
        )
    return ModelChain(pairs)
