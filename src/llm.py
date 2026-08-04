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

DEFAULT_PROVIDER = "anthropic"

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
        provider: str = DEFAULT_PROVIDER,
        base_url: str | None = None,
        session: Any = None,
        max_retries: int = DEFAULT_MAX_RETRIES,
        sleep: Callable[[float], None] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> None:
        self.provider = str(provider or DEFAULT_PROVIDER).strip().lower()
        if self.provider not in PROVIDERS:
            raise LLMError(
                f"unknown llm.provider {provider!r} — expected one of "
                f"{', '.join(PROVIDERS)}"
            )
        self.api_key = str(api_key or "")
        self.max_retries = max(0, int(max_retries))
        self._sleep = sleep or time.sleep
        self._injected = client
        self._session = session
        self._real: Any = None
        self._extra_headers = dict(headers or {})
        self.base_url = str(
            base_url or (OPENROUTER_BASE_URL if self.provider == "openrouter" else "")
        ).rstrip("/")

        # A key is only dispensable when a seam replaces the transport.
        has_seam = client is not None or (
            self.provider == "openrouter" and session is not None
        )
        if not has_seam and not self.api_key:
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
                        max_tokens: int, temperature: float) -> str:
        response = self.client.messages.create(
            model=model,
            max_tokens=max_tokens,
            temperature=temperature,
            system=system,
            messages=[{"role": "user", "content": prompt}],
        )
        return _message_text(response)

    def _call_openrouter(self, *, model: str, system: str, prompt: str,
                         max_tokens: int, temperature: float) -> str:
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
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            # Attribution headers OpenRouter asks for; nothing user-identifying.
            "HTTP-Referer": OPENROUTER_REFERER,
            "X-Title": OPENROUTER_TITLE,
        }
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
        return _openrouter_text(data)

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
    ) -> str:
        """One completion, returned as plain text.

        Retries transient failures up to `max_retries` times with exponential
        backoff; auth and 4xx errors are re-raised immediately as `LLMError`
        because they will never succeed. Never raises anything but `LLMError`.
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
                            max_tokens=max_tokens, temperature=temperature)
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
                    raise LLMError(f"{model} call failed: {exc}") from exc
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
        sleep: Callable[[float], None] | None = None,
    ) -> dict[str, Any]:
        """`complete()` plus `extract_json()`.

        `schema_hint` is appended to the prompt when given — a literal example
        of the wanted object buys more compliance than any amount of prose.
        """
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


def extract_json(text: str, *, require_keys: Iterable[str] | None = None) -> dict[str, Any]:
    """Recover a JSON object from a model reply.

    `require_keys` is the defence against a planted object. A job ad can carry
    a complete, valid-looking score object, and a model that quotes the
    instruction it is refusing — "the posting asks me to return {...}, but my
    assessment is {...}" — hands this function two parseable objects with the
    attacker's first.

    Matching is on the FULL key set, not any of them: the plant in a real ad
    usually carries `score` and `verdict`, so "has one of the keys" does not
    discriminate at all. The model's own answer is the one that carries every
    key the prompt asked for.

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
    parsed_any: dict[str, Any] | None = None

    for chunk in _candidate_chunks(raw):
        candidate = _try_load(chunk)
        if candidate is None:
            continue
        if wanted and wanted.issubset(candidate):
            return candidate
        if parsed_any is None:
            parsed_any = candidate

    if parsed_any is not None:
        if wanted:
            logger.warning(
                "no object in the model reply carried the full expected key set "
                "(%s) — passing the first one through unjudged",
                ", ".join(sorted(wanted)),
            )
        return parsed_any

    preview = str(raw).strip().replace("\n", " ")[:200]
    raise LLMError(f"no JSON object found in model response: {preview!r}")


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
