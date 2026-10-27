import httpx
import openai
import pytest
from pydantic import BaseModel

from app import llm


class Ping(BaseModel):
    ok: bool


class FakeResponse:
    def __init__(self, text, status="completed"):
        self.output_text, self.status = text, status


class FakeClient:
    """Stands in for OpenAI(); `script` items are response texts or exceptions to raise."""

    def __init__(self, script):
        self.script, self.calls = list(script), 0
        self.responses = self

    def create(self, **kwargs):
        self.calls += 1
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item if isinstance(item, FakeResponse) else FakeResponse(item)


@pytest.fixture
def install(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    monkeypatch.setenv("OPENROUTER_MODEL", "test/model")
    monkeypatch.setattr(llm.time, "sleep", lambda s: None)

    def _install(*script):
        client = FakeClient(script)
        monkeypatch.setattr(llm, "_make_client", lambda settings: client)
        return client

    return _install


def conn_error():
    return openai.APIConnectionError(request=httpx.Request("POST", "http://x"))


def test_extract_json_handles_fences_and_chatter():
    assert llm.extract_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert llm.extract_json('Sure! Here it is: {"a": {"b": 2}} Hope that helps') == {"a": {"b": 2}}
    with pytest.raises(ValueError):
        llm.extract_json("no json here")


def test_missing_key_or_model_raises_config_error(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    with pytest.raises(llm.LLMConfigError):
        llm.generate_text("s", "u")


def test_transient_error_retried_once_then_succeeds(install):
    client = install(conn_error(), "hello")
    assert llm.generate_text("s", "u", retries=1) == "hello"
    assert client.calls == 2


def test_transient_error_is_bounded(install):
    client = install(conn_error(), conn_error(), "never reached")
    with pytest.raises(llm.LLMError):
        llm.generate_text("s", "u", retries=1)
    assert client.calls == 2


def test_non_transient_error_is_not_retried(install):
    response = httpx.Response(400, request=httpx.Request("POST", "http://x"))
    client = install(openai.BadRequestError("bad", response=response, body=None), "unused")
    with pytest.raises(llm.LLMError):
        llm.generate_text("s", "u")
    assert client.calls == 1


def test_truncated_and_empty_output_raise(install):
    install(FakeResponse('{"ok": tr', status="incomplete"))
    with pytest.raises(llm.LLMOutputError):
        llm.generate_text("s", "u")
    install("   ")
    with pytest.raises(llm.LLMOutputError):
        llm.generate_text("s", "u")


def test_structured_valid_first_time(install):
    client = install('{"ok": true}')
    assert llm.generate_structured("s", "u", Ping).ok is True
    assert client.calls == 1


def test_structured_one_repair_then_success(install):
    client = install("not json at all", '{"ok": true}')
    assert llm.generate_structured("s", "u", Ping).ok is True
    assert client.calls == 2


def test_structured_gives_up_after_exactly_one_repair(install):
    client = install('{"ok": "maybe"}', "still wrong", '{"ok": true}')
    with pytest.raises(llm.LLMOutputError):
        llm.generate_structured("s", "u", Ping)
    assert client.calls == 2


def test_repair_prompt_loads():
    assert "# Role" in llm.load_prompt("repair")
    with pytest.raises(ValueError):
        llm.load_prompt("../secrets")


# ---------- extract_json edge cases and Settings validation ----------

def test_extract_json_skips_stray_braces_before_the_object():
    assert llm.extract_json('Use {braces} like so: {"a": 1} thanks') == {"a": 1}
    assert llm.extract_json('{ not json } then {"a": {"b": [1, 2]}}') == {"a": {"b": [1, 2]}}


def test_extract_json_handles_nested_fences():
    assert llm.extract_json('```json\n```json\n{"a": 1}\n```\n```') == {"a": 1}


@pytest.mark.parametrize("text", ['[{"a": 1}]', "[1, 2]", "42", "just words", "{unterminated"])
def test_extract_json_rejects_arrays_scalars_and_garbage(text):
    with pytest.raises(ValueError):
        llm.extract_json(text)


@pytest.mark.xfail(strict=True, reason="Known limit: a complete JSON object inside the chatter wins over the real one")
def test_extract_json_known_limit_example_object_in_chatter():
    assert llm.extract_json('For example {"x": 1}. Final answer: {"a": 2}') == {"a": 2}


def test_settings_defaults_and_overrides(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "k")
    monkeypatch.setenv("OPENROUTER_MODEL", "m")
    for name in ("LLM_TIMEOUT_SECONDS", "LLM_MAX_OUTPUT_TOKENS", "LLM_DRAFT_MAX_OUTPUT_TOKENS"):
        monkeypatch.delenv(name, raising=False)
    cfg = llm.get_settings()
    assert (cfg.timeout, cfg.max_output_tokens, cfg.draft_max_output_tokens) == (90.0, 6000, 12000)
    monkeypatch.setenv("LLM_TIMEOUT_SECONDS", " 12.5 ")
    monkeypatch.setenv("LLM_MAX_OUTPUT_TOKENS", "800")
    assert (llm.get_settings().timeout, llm.get_settings().max_output_tokens) == (12.5, 800)
    monkeypatch.setenv("LLM_MAX_OUTPUT_TOKENS", "1.5")  # not an integer
    with pytest.raises(llm.LLMConfigError):
        llm.get_settings()


def test_blank_key_or_model_is_a_config_error(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "   ")
    monkeypatch.setenv("OPENROUTER_MODEL", "m")
    with pytest.raises(llm.LLMConfigError):
        llm.get_settings()
    monkeypatch.setenv("OPENROUTER_API_KEY", "k")
    monkeypatch.setenv("OPENROUTER_MODEL", "")
    with pytest.raises(llm.LLMConfigError):
        llm.get_settings()
    assert llm.provider_info() == ("openrouter", "unconfigured")


def test_max_output_tokens_override_reaches_the_client(install, monkeypatch):
    seen = {}
    client = install("hello")
    original = client.create
    client.create = lambda **kw: (seen.update(kw), original(**kw))[1]
    llm.generate_text("s", "u", max_output_tokens=321)
    assert seen["max_output_tokens"] == 321 and seen["model"] == "test/model"


# ---------- exponential backoff + jitter + 401/429 retryable ----------

def auth_error_with_status(status_code: int, msg: str = "free tier rate limit exceeded"):
    response = httpx.Response(status_code, request=httpx.Request("POST", "http://x"))
    return openai.AuthenticationError(msg, response=response, body=None)


def test_default_retries_is_three(install):
    """With the default retries=3, three transient errors are retried and a 4th succeeds."""
    client = install(conn_error(), conn_error(), conn_error(), "success")
    assert llm.generate_text("s", "u") == "success"
    assert client.calls == 4


def test_default_retries_exhausted_raises(install):
    """Four transient errors exhaust the default retries=3 budget."""
    client = install(conn_error(), conn_error(), conn_error(), conn_error(), "never")
    with pytest.raises(llm.LLMError):
        llm.generate_text("s", "u")
    assert client.calls == 4


def test_401_free_tier_is_retried(install):
    """A 401 from a free-tier provider should be treated as transient and retried."""
    client = install(auth_error_with_status(401), "recovered")
    assert llm.generate_text("s", "u", retries=1) == "recovered"
    assert client.calls == 2


def test_429_is_retried(install):
    """A 429 rate-limit response should be retried."""
    client = install(auth_error_with_status(429), "recovered")
    assert llm.generate_text("s", "u", retries=1) == "recovered"
    assert client.calls == 2


def test_real_auth_error_400_not_retried(install):
    """A 400 bad-request must not be retried — it is a permanent error."""
    response = httpx.Response(400, request=httpx.Request("POST", "http://x"))
    client = install(openai.BadRequestError("bad", response=response, body=None), "unused")
    with pytest.raises(llm.LLMError):
        llm.generate_text("s", "u")
    assert client.calls == 1


def test_bad_key_401_is_not_retried(install):
    """A genuine auth failure 401 (wrong key, no rate/quota hint) must not be retried."""
    client = install(auth_error_with_status(401, msg="invalid api key"), "unused")
    with pytest.raises(llm.LLMError):
        llm.generate_text("s", "u")
    assert client.calls == 1


def test_backoff_sleep_is_called_on_retry(install, monkeypatch):
    """Each retry should trigger a sleep call (jitter aside, at least one sleep per retry)."""
    sleep_calls = []
    monkeypatch.setattr(llm.time, "sleep", lambda s: sleep_calls.append(s))
    client = install(conn_error(), conn_error(), "ok")
    llm.generate_text("s", "u", retries=2)
    assert len(sleep_calls) == 2  # one sleep per retry attempt
