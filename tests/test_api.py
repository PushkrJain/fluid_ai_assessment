import re

import pytest

from app import handlers
from app.schemas import AgentRequest
from tests.conftest import REQUEST, script_happy

RESPONSE_FIELDS = {
    "run_id": str, "status": str, "summary": str, "document_type": str, "plan": list, "assumptions": list,
    "open_questions": list, "review": dict, "revisions_performed": int, "fallback_used": bool, "warnings": list,
    "provider": str, "model": str, "document_filename": str, "download_url": str,
}


def test_health_states(fake_llm, client, monkeypatch):
    assert client.get("/health").json() == {"status": "ok", "llm": "configured"}
    monkeypatch.delenv("OPENROUTER_MODEL")
    assert client.get("/health").json() == {"status": "ok", "llm": "not_configured"}


@pytest.mark.parametrize("payload", [
    {}, {"request": 123}, {"request": None}, {"request": ["a long enough string"]},
    {"request": "123456789"}, {"request": "x" * 5001}, {"request": " " * 50}, {"other": "a long enough string"},
])
def test_invalid_bodies_are_422(client, payload):
    assert client.post("/agent", json=payload).status_code == 422


def test_non_json_body_is_422(client):
    assert client.post("/agent", content=b"not json", headers={"content-type": "application/json"}).status_code == 422


def test_length_is_judged_after_trimming():
    assert AgentRequest(request="   1234567890   ").request == "1234567890"
    assert AgentRequest(request="x" * 5000).request == "x" * 5000


def test_ten_character_request_after_trimming_reaches_the_agent(happy, client):
    assert client.post("/agent", json={"request": "  1234567890  "}).status_code == 200


def test_happy_response_has_every_field_with_the_right_type(happy, client):
    body = client.post("/agent", json=REQUEST).json()
    for name, kind in RESPONSE_FIELDS.items():
        assert isinstance(body[name], kind), name
    assert body["error"] is None and re.fullmatch(r"[0-9a-f]{32}", body["run_id"])
    assert set(body["plan"][0]) == {"id", "title", "description", "handler", "dependencies", "expected_output",
                                    "status", "result_summary", "error"}
    assert client.get(body["download_url"]).status_code == 200


def test_identical_requests_get_distinct_runs_and_files(fake_llm, client):
    script_happy(fake_llm)
    script_happy(fake_llm)
    first = client.post("/agent", json=REQUEST).json()
    second = client.post("/agent", json=REQUEST).json()
    assert first["run_id"] != second["run_id"] and first["document_filename"] != second["document_filename"]


def test_not_configured_is_503_and_failed_has_no_download(monkeypatch, tmp_path, client):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.setenv("OUTPUT_DIR", str(tmp_path))
    r = client.post("/agent", json=REQUEST)
    body = r.json()
    assert r.status_code == 503 and body["status"] == "failed"
    assert body["download_url"] is None and body["document_filename"] is None


def test_document_build_failure_is_failed_503_without_a_download(happy, client, monkeypatch):
    def broken(spec):
        raise OSError("disk full: /secret/path")
    monkeypatch.setattr(handlers, "build_docx", broken)
    r = client.post("/agent", json=REQUEST)
    body = r.json()
    assert r.status_code == 503 and body["status"] == "failed" and body["download_url"] is None
    assert "/secret/path" not in r.text  # safe error text only
    assert body["plan"][-1]["status"] == "failed"


def test_unhandled_exception_is_a_generic_500(client, monkeypatch):
    import app.main

    def boom(text):
        raise RuntimeError("secret internal detail")
    monkeypatch.setattr(app.main, "run_agent", boom)
    r = client.post("/agent", json=REQUEST)
    assert r.status_code == 500 and "secret" not in r.text and "Internal error" in r.json()["detail"]
