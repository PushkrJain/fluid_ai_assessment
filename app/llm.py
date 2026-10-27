"""Thin OpenRouter wrapper (OpenAI SDK, Responses API).

Responsibilities: configuration, bounded retries on transient errors, JSON extraction,
and ONE repair attempt for malformed/invalid structured output. Nothing else.
Prompts and request content are never logged.
"""
import json
import logging
import math
import os
import random
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TypeVar

import openai
from dotenv import load_dotenv
from openai import OpenAI
from pydantic import BaseModel

load_dotenv()
logger = logging.getLogger("agent.llm")

PROVIDER = "openrouter"
BASE_URL = "https://openrouter.ai/api/v1"
PROMPT_DIR = Path(__file__).parent / "prompts"
TRANSIENT_ERRORS = (
    openai.APIConnectionError,  # includes APITimeoutError
    openai.RateLimitError,
    openai.InternalServerError,
)

# Some free-tier providers (including OpenRouter) return 401 or 429 when a per-model
# rate limit is exhausted mid-run rather than a real auth failure. We detect these by
# inspecting the HTTP status code and treat them as transient so the retry loop handles them.
_RETRYABLE_HTTP_CODES = {401, 429}

# Exponential backoff parameters
_BACKOFF_BASE = 2.0       # seconds for the first retry
_BACKOFF_MAX = 60.0       # cap so retries don't wait forever
_JITTER_FACTOR = 0.25     # ± 25 % random jitter on each sleep


def _is_retryable(exc: openai.OpenAIError) -> bool:
    """Return True for errors that are worth retrying (transient or free-tier rate limits).

    OpenRouter free-tier exhaustion comes back as a 401 with a message that mentions
    'rate' or 'limit' or 'quota'.  A genuine authentication failure (wrong key) also
    returns 401 but with a different message.  We inspect the message so real auth
    failures are still treated as permanent.
    """
    if isinstance(exc, TRANSIENT_ERRORS):
        return True
    status = getattr(getattr(exc, "response", None), "status_code", None)
    if status == 429:
        return True
    if status == 401:
        # Only retry if the message hints at a rate/quota limit, not a bad key
        msg = str(exc).lower()
        return any(word in msg for word in ("rate", "limit", "quota", "capacity", "free"))
    return False


def _backoff_sleep(attempt: int) -> None:
    """Exponential backoff with full jitter: sleep for a random value in [0, cap]."""
    cap = min(_BACKOFF_BASE * (2 ** (attempt - 1)), _BACKOFF_MAX)
    jitter = random.uniform(0, cap * _JITTER_FACTOR * 2)  # symmetric ± jitter
    delay = cap + jitter - (cap * _JITTER_FACTOR)          # centre on cap, add noise
    delay = max(0.0, delay)
    logger.info("llm_backoff attempt=%d sleep=%.1fs", attempt, delay)
    time.sleep(delay)


T = TypeVar("T", bound=BaseModel)


class LLMError(Exception):
    """The LLM could not be used (unavailable, rejected the request, ...)."""


class LLMConfigError(LLMError):
    """Missing or invalid configuration (e.g. API key)."""


class LLMOutputError(LLMError):
    """The model answered, but the output was empty, truncated or invalid."""


@dataclass(frozen=True)
class Settings:
    api_key: str
    model: str
    timeout: float
    max_output_tokens: int
    draft_max_output_tokens: int


def _positive(name: str, default: float, cast):
    """Read a positive numeric env var. A variable that is SET but empty or invalid is an error
    (never silently replaced by the default). The message names the variable, never its value."""
    raw = os.environ.get(name)
    if raw is None:
        return cast(default)
    try:
        value = cast(raw.strip())
    except ValueError:
        raise LLMConfigError(f"{name} must be a positive number") from None
    if not math.isfinite(value) or value <= 0:
        raise LLMConfigError(f"{name} must be a positive number")
    return value


def get_settings() -> Settings:
    api_key = os.getenv("OPENROUTER_API_KEY", "").strip()
    model = os.getenv("OPENROUTER_MODEL", "").strip()
    if not api_key:
        raise LLMConfigError("OPENROUTER_API_KEY is not set")
    if not model:
        raise LLMConfigError("OPENROUTER_MODEL is not set")
    return Settings(
        api_key=api_key,
        model=model,
        timeout=_positive("LLM_TIMEOUT_SECONDS", 90, float),
        max_output_tokens=_positive("LLM_MAX_OUTPUT_TOKENS", 6000, int),
        draft_max_output_tokens=_positive("LLM_DRAFT_MAX_OUTPUT_TOKENS", 12000, int),
    )


def provider_info() -> tuple[str, str]:
    """(provider, model) for responses and logs; never raises."""
    return PROVIDER, os.getenv("OPENROUTER_MODEL", "").strip() or "unconfigured"


def load_prompt(name: str) -> str:
    """Load a RICE-format Markdown prompt from app/prompts/<name>.md."""
    if not name.isidentifier():
        raise ValueError(f"Invalid prompt name: {name!r}")
    return (PROMPT_DIR / f"{name}.md").read_text(encoding="utf-8")


def _make_client(settings: Settings) -> OpenAI:
    # max_retries=0: all retrying is done (and bounded) here, not hidden inside the SDK.
    return OpenAI(
        base_url=BASE_URL,
        api_key=settings.api_key,
        timeout=settings.timeout,
        max_retries=0,
    )


def generate_text(
    system: str,
    user: str,
    *,
    max_output_tokens: int | None = None,
    retries: int = 3,
    backoff: float = _BACKOFF_BASE,
) -> str:
    """One model call with exponential backoff + jitter for transient failures.

    `retries` is the number of *extra* attempts after the first, so the total
    attempts is retries + 1.  Free-tier 401/429 responses are treated as
    transient and are retried the same way as RateLimitError.
    """
    settings = get_settings()
    client = _make_client(settings)
    limit = max_output_tokens or settings.max_output_tokens

    for attempt in range(1, retries + 2):
        started = time.monotonic()
        try:
            response = client.responses.create(
                model=settings.model,
                input=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                max_output_tokens=limit,
            )
        except openai.OpenAIError as exc:
            if _is_retryable(exc) and attempt <= retries:
                logger.warning(
                    "llm_transient_error attempt=%d/%d error=%s status=%s -> retrying",
                    attempt, retries + 1, type(exc).__name__,
                    getattr(getattr(exc, "response", None), "status_code", "?"),
                )
                _backoff_sleep(attempt)
                continue
            if _is_retryable(exc):
                raise LLMError(
                    f"LLM unavailable after {attempt} attempt(s) ({type(exc).__name__})"
                ) from exc
            # Non-retryable: auth (real), bad request, unknown model, ...
            raise LLMError(f"LLM request rejected ({type(exc).__name__})") from exc

        text = (response.output_text or "").strip()
        status = getattr(response, "status", None)
        logger.info(
            "llm_call model=%s attempt=%d seconds=%.1f status=%s chars=%d",
            settings.model, attempt, time.monotonic() - started, status, len(text),
        )
        if status == "incomplete":
            raise LLMOutputError("Model output was truncated; raise LLM_MAX_OUTPUT_TOKENS")
        if not text:
            raise LLMOutputError("Model returned empty output")
        return text

    raise LLMError("LLM call failed")  # unreachable; keeps type checkers satisfied


def extract_json(text: str) -> dict:
    """Return the first decodable JSON OBJECT in model text.

    Tolerates code fences and chatter (including stray braces) before the object. Known limit:
    if the chatter itself contains a complete, valid JSON object, that one is returned instead.
    A top-level array is rejected on purpose: every schema here expects an object."""
    cleaned = text.strip()
    if cleaned.startswith("["):
        raise ValueError("Model output is a JSON array, expected an object")
    decoder = json.JSONDecoder()
    position = cleaned.find("{")
    while position != -1:
        try:
            obj, _ = decoder.raw_decode(cleaned[position:])
        except ValueError:
            position = cleaned.find("{", position + 1)
            continue
        if isinstance(obj, dict):
            return obj
        position = cleaned.find("{", position + 1)
    raise ValueError("No JSON object found in model output")


def _repair_message(model_cls: type[BaseModel], raw: str, error: Exception) -> str:
    return (
        f"## Validation error\n{str(error)[:2000]}\n\n"
        f"## Previous output\n{raw[:12000]}\n\n"
        f"## Required JSON schema\n{json.dumps(model_cls.model_json_schema())}"
    )


def generate_structured(
    system: str,
    user: str,
    model_cls: type[T],
    *,
    max_output_tokens: int | None = None,
) -> T:
    """Call the model, validate against `model_cls`, and allow exactly one repair attempt."""
    raw = generate_text(system, user, max_output_tokens=max_output_tokens)
    try:
        return model_cls.model_validate(extract_json(raw))
    except ValueError as first_error:  # pydantic.ValidationError is a ValueError
        logger.warning(
            "llm_invalid_output schema=%s error_type=%s -> one repair attempt",
            model_cls.__name__, type(first_error).__name__,
        )
        repaired = generate_text(
            load_prompt("repair"),
            _repair_message(model_cls, raw, first_error),
            max_output_tokens=max_output_tokens,
        )
        try:
            return model_cls.model_validate(extract_json(repaired))
        except ValueError as second_error:
            raise LLMOutputError(
                f"Model output invalid after one repair attempt ({type(second_error).__name__})"
            ) from second_error


if __name__ == "__main__":
    # Live smoke check:  python -m app.llm
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")

    class Ping(BaseModel):
        ok: bool
        message: str

    cfg = get_settings()
    print("Model:", cfg.model)
    print("Text check:", generate_text(
        "You are a test assistant.", "Reply with exactly: Responses API works.",
        max_output_tokens=300,
    ))
    ping = generate_structured(
        "You return only JSON, with no Markdown fences and no commentary.",
        'Return exactly this JSON object: {"ok": true, "message": "hello"}',
        Ping,
        max_output_tokens=300,
    )
    print("JSON check:", ping)
