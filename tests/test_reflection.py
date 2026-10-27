"""Reflection loop behaviour. (Known-gap, deterministic-gap, revision-cap and failed-revision cases
live in test_agent.py / test_regressions.py; these cover the remaining paths.)"""
from app.handlers import MAX_REVISION_TARGETS
from tests.conftest import (REQUEST, docx_text, make_analysis, make_draft, make_plan, make_review, section)


def base(fake_llm, draft=None):
    fake_llm.script("planner", make_plan()).script("analyzer", make_analysis()).script("drafter", draft or make_draft())


def test_clean_first_review_means_no_revision_and_one_review_call(fake_llm, client):
    base(fake_llm)
    fake_llm.script("reviewer", make_review(score=92))
    fake_llm.script("reviser", {"summary": "unused", "sections": [section("Scope")]})
    body = client.post("/agent", json=REQUEST).json()
    assert body["revisions_performed"] == 0 and fake_llm.count("reviewer") == 1
    assert fake_llm.queues["reviser"], "the reviser must not have been called"
    assert body["summary"].count("Review score 92/100") == 1  # finalized: score is presented as final


def test_low_score_without_named_sections_triggers_a_capped_broad_revision(fake_llm, client):
    headings = tuple(["Executive Summary", "Scope", "Risks and Mitigations"] + [f"Extra {i}" for i in range(5)])
    base(fake_llm, make_draft(headings))
    fake_llm.script("reviewer", make_review(score=50, failed_checks=["Coverage"]), make_review(score=80))
    fake_llm.script("reviser", {"summary": "ok", "sections": [section("Executive Summary", content="Rewritten.")]})
    body = client.post("/agent", json=REQUEST).json()
    targets = fake_llm.context("reviser")["targets"]
    assert len(targets) == MAX_REVISION_TARGETS and targets == list(headings[:MAX_REVISION_TARGETS])
    assert body["revisions_performed"] == 1


def test_persistent_gap_stays_visible_in_response_and_document_with_no_second_revision(fake_llm, client, tmp_path):
    base(fake_llm)
    fake_llm.script("reviewer", make_review(score=60, failed_checks=["x"], sections_to_revise=["Scope"]),
                    make_review(score=70, remaining_issues=["Budget owner still unconfirmed"]))
    fake_llm.script("reviser", {"summary": "1", "sections": [section("Scope", content="Better.")]},
                    {"summary": "2", "sections": [section("Scope", content="Even better.")]})
    body = client.post("/agent", json=REQUEST).json()
    assert body["revisions_performed"] == 1 and len(fake_llm.queues["reviser"]) == 1  # second reviser item unused
    assert "Budget owner still unconfirmed" in body["review"]["remaining_issues"]
    assert "Budget owner still unconfirmed" in docx_text(tmp_path / body["document_filename"])
    assert body["status"] == "completed_with_warnings"
