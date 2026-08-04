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
from typing import Any, Callable, Iterator

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


class LLMError(RuntimeError):
    """Any failure to get a usable answer out of the model."""


# --------------------------------------------------------------------------
# client
# --------------------------------------------------------------------------


def _status_code(exc: BaseException) -> int | None:
    """Best-effort HTTP status from an SDK exception, or None."""
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
    return None


def _is_transient(exc: BaseException) -> bool:
    """Should this failure be retried?

    An explicit status wins over the class name: a 400 named
    `InternalServerError` is still a malformed request, and retrying it just
    burns the budget. Only when there is no status do we fall back to the name
    (that is the `APIConnectionError` / socket-level case, which has none).
    """
    status = _status_code(exc)
    if status is not None:
        return status in RETRYABLE_STATUS
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
    """Anthropic wrapper with retries, injectable for tests.

    `client=` is the seam: pass anything exposing `.messages.create(**kwargs)`
    and no SDK import happens. Without it the real client is built lazily on
    first use.
    """

    def __init__(
        self,
        api_key: str,
        *,
        client: Any = None,
        max_retries: int = DEFAULT_MAX_RETRIES,
        sleep: Callable[[float], None] | None = None,
    ) -> None:
        self.api_key = str(api_key or "")
        self.max_retries = max(0, int(max_retries))
        self._sleep = sleep or time.sleep
        self._injected = client
        self._real: Any = None
        if client is None and not self.api_key:
            # Fail here rather than three stages later with a stack trace from
            # inside the SDK. `config.validate` catches this first in practice.
            raise LLMError(
                "No Anthropic API key — set keys.anthropic in config.yaml or "
                "export ANTHROPIC_API_KEY"
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
                response = self.client.messages.create(
                    model=model,
                    max_tokens=max_tokens,
                    temperature=temperature,
                    system=system,
                    messages=[{"role": "user", "content": prompt}],
                )
            except LLMError:
                raise
            except Exception as exc:
                last = exc
                if not _is_transient(exc):
                    raise LLMError(f"{model} call failed: {exc}") from exc
                logger.warning(
                    "transient LLM error from %s (attempt %d/%d): %s",
                    model, attempt + 1, attempts, exc,
                )
                continue

            text = _message_text(response)
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
            )
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


def extract_json(text: str) -> dict[str, Any]:
    """Recover a JSON object from whatever the model actually sent.

    Handles, in order of preference: a ```json fenced block, a bare ``` block,
    the raw text, and finally any balanced `{...}` span embedded in prose.
    Raises `LLMError` when nothing parses — callers treat that as a failed
    call rather than guessing.
    """
    if not text or not str(text).strip():
        raise LLMError("model returned an empty response")

    raw = str(text)
    for chunk in [*_fenced_blocks(raw), raw]:
        parsed = _try_load(chunk)
        if parsed is not None:
            return parsed
        for span in _balanced_objects(chunk):
            parsed = _try_load(span)
            if parsed is not None:
                return parsed

    preview = raw.strip().replace("\n", " ")[:200]
    raise LLMError(f"no JSON object found in model response: {preview!r}")
