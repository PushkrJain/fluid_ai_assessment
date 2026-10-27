"""Neither request content nor secrets may leak into logs, warnings or error fields.
Uses the client-level fake so the REAL llm.generate_text logging path runs."""
import httpx
import openai
import pytest

from app import llm
from tests.conftest import script_happy

CANARY = "ZEBRA-CANARY-7731"
KEY = "sk-test-secret-key-123456"
REQUEST = {"request": f"Create a project plan for the {CANARY} portal launch in eight weeks."}


def assert_clean(body, caplog):
    blob = caplog.text + str(body["warnings"]) + str(body["error"])
    assert CANARY not in blob and KEY not in blob


def test_happy_path_leaks_nothing(fake_client, client, caplog):
    caplog.set_level("DEBUG")
    script_happy(fake_client)
    body = client.post("/agent", json=REQUEST).json()
    assert body["status"] in ("completed", "completed_with_warnings")
    assert KEY not in str(body)  # the key must never appear anywhere in the response
    assert_clean(body, caplog)
    assert "llm_call" in caplog.text  # proves the real wrapper logged (and still leaked nothing)


@pytest.mark.parametrize("failure", [
    openai.APIConnectionError(request=httpx.Request("POST", "http://x")),
    openai.BadRequestError(f"bad request, key={KEY}, text={CANARY}",
                           response=httpx.Response(400, request=httpx.Request("POST", "http://x")), body=None),
])
def test_failure_paths_leak_nothing(fake_client, client, caplog, failure):
    caplog.set_level("DEBUG")
    # Supply enough copies of the failure to exhaust all retry attempts (default retries=3
    # means up to 4 total attempts for transient errors; non-transient errors stop immediately).
    fake_client.script("planner", failure, failure, failure, failure)
    body = client.post("/agent", json=REQUEST).json()
    assert body["fallback_used"] is True
    assert_clean(body, caplog)


def test_provider_error_text_never_reaches_the_exception_message(fake_client):
    fake_client.script("planner", openai.BadRequestError(
        f"echo {KEY}", response=httpx.Response(400, request=httpx.Request("POST", "http://x")), body=None))
    with pytest.raises(llm.LLMError) as info:
        llm.generate_text(llm.load_prompt("planner"), "user")
    assert KEY not in str(info.value)


def test_prompts_and_user_text_are_not_logged(fake_client, caplog):
    caplog.set_level("DEBUG")
    fake_client.script("analyzer", '{"summary": "ok"}')
    llm.generate_text(llm.load_prompt("analyzer"), f"PRIVATE-USER-TEXT {CANARY}")
    assert "PRIVATE-USER-TEXT" not in caplog.text and CANARY not in caplog.text
    assert "# Role" not in caplog.text and KEY not in caplog.text
