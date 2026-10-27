"""Regression tests for the verified defects. Each was written BEFORE its fix."""
import httpx
import openai
import pytest
from pydantic import ValidationError

from app import handlers, llm
from app.agent import completed_stages
from app.document import (DocumentSpec, KNOWN_FACTS_HEADING, ASSUMPTIONS_HEADING, DISCLOSURE_HEADING,
                          build_docx)
from app.schemas import AgentResponse, DocumentContent, DocumentSection, RevisionResult
from tests.conftest import (REQUEST, docx_headings, docx_text, make_analysis, make_draft, make_plan, make_review,
                            make_state, section)

PLANNING_FAILED = "No AI planning, analysis, drafting or review was completed for this run."


# ---------------- Fix 1: fallback wording must reflect what actually happened ----------------

def test_fix1_planning_failure_banner(fake_llm, client, tmp_path):
    fake_llm.script("planner", llm.LLMError("LLM unavailable after 2 attempts"))
    body = client.post("/agent", json=REQUEST).json()
    assert PLANNING_FAILED in body["summary"]
    assert PLANNING_FAILED in docx_text(tmp_path / body["document_filename"])


def test_fix1_drafting_failure_lists_completed_stages(fake_llm, client, tmp_path):
    fake_llm.script("planner", make_plan()).script("analyzer", make_analysis())
    fake_llm.script("drafter", llm.LLMError("LLM unavailable"))
    body = client.post("/agent", json=REQUEST).json()
    text = docx_text(tmp_path / body["document_filename"])
    for source in (body["summary"], text):
        assert "Completed stages: planning, analysis, assumption resolution." in source
        assert "Failed stage: drafting." in source
        assert "No AI planning" not in source
        assert "no AI planning" not in source


def test_fix1_summary_and_docx_banner_agree(fake_llm, client, tmp_path):
    fake_llm.script("planner", make_plan()).script("analyzer", make_analysis()).script("drafter", llm.LLMError("x"))
    body = client.post("/agent", json=REQUEST).json()
    notice = "AI-generated content could not be completed. Completed stages: planning, analysis, assumption resolution. Failed stage: drafting."
    assert notice in body["summary"] and notice in docx_text(tmp_path / body["document_filename"])


def test_fix1_completed_stages_helper():
    state = make_state()
    assert completed_stages(state) == ["planning"]
    state.tasks[0].status = "completed"
    state.tasks[1].status = "failed"
    assert completed_stages(state) == ["planning", "analysis"]


# ---------------- Fix 2: reviser may only change approved targets ----------------

def reviser(*sections, summary="Revised."):
    return {"summary": summary, "sections": list(sections)}


def run_with_review(fake_llm, client, first_review, revision, draft=None, final_review=None):
    fake_llm.script("planner", make_plan()).script("analyzer", make_analysis())
    fake_llm.script("drafter", draft or make_draft())
    fake_llm.script("reviewer", first_review, *([final_review] if final_review else []))
    fake_llm.script("reviser", revision)
    return client.post("/agent", json=REQUEST).json()


def test_fix2_unrelated_heading_rejects_the_whole_revision(fake_llm, client, tmp_path):
    first = make_review(score=60, identified_gaps=["Scope is thin"], failed_checks=["Coverage"], sections_to_revise=["Scope"])
    body = run_with_review(fake_llm, client, first, reviser(section("Scope"), section("Unrelated Budget Section")))
    assert body["revisions_performed"] == 0
    assert any("Revision rejected" in w for w in body["warnings"])
    assert "Unrelated Budget Section" not in docx_headings(tmp_path / body["document_filename"])
    assert "Scope is thin" in body["review"]["remaining_issues"]
    assert fake_llm.count("reviewer") == 1 and fake_llm.count("reviser") == 1  # no retry, no final check


def test_fix2_silent_rewrite_of_non_target_section_is_rejected(fake_llm, client):
    first = make_review(score=60, failed_checks=["x"], sections_to_revise=["Scope"])
    body = run_with_review(fake_llm, client, first, reviser(section("Executive Summary", content="Quietly rewritten")))
    assert body["revisions_performed"] == 0 and any("Revision rejected" in w for w in body["warnings"])


def test_fix2_validator_rejects_duplicate_headings_changed_title_and_changed_non_target():
    state = make_state()
    original = state.draft
    dup = DocumentContent(title=original.title, sections=original.sections + [DocumentSection(heading="Scope", content="again")])
    with pytest.raises(handlers.RevisionRejected, match="duplicate"):
        handlers._validate_revised(original, dup, touched={len(original.sections)})
    retitled = DocumentContent(title="Another title", sections=original.sections)
    with pytest.raises(handlers.RevisionRejected, match="title"):
        handlers._validate_revised(original, retitled, touched=set())
    changed = DocumentContent(title=original.title, sections=[DocumentSection(heading="Executive Summary", content="changed")] + original.sections[1:])
    with pytest.raises(handlers.RevisionRejected, match="not an approved target"):
        handlers._validate_revised(original, changed, touched=set())


def test_fix2_validator_does_not_punish_pre_existing_similar_headings():
    state = make_state(headings=("Scope", "Scope and Exclusions", "Risks and Mitigations"))
    same = DocumentContent(title=state.draft.title, sections=list(state.draft.sections))
    handlers._validate_revised(state.draft, same, touched=set())  # must not raise


def test_fix2_system_section_named_as_target_is_filtered_and_never_duplicated(fake_llm, client, tmp_path):
    first = make_review(score=85, sections_to_revise=["Assumptions and Open Questions"])
    fake_llm.script("planner", make_plan()).script("analyzer", make_analysis()).script("drafter", make_draft())
    fake_llm.script("reviewer", first)
    body = client.post("/agent", json=REQUEST).json()
    assert fake_llm.count("reviser") == 0  # nothing left to revise after filtering
    assert docx_headings(tmp_path / body["document_filename"]).count(ASSUMPTIONS_HEADING) == 1


def test_fix2_system_target_is_dropped_but_real_targets_still_revised(fake_llm, client, tmp_path):
    first = make_review(score=60, failed_checks=["x"], sections_to_revise=["Assumptions and Open Questions", "Scope"])
    body = run_with_review(fake_llm, client, first, reviser(section("Scope", content="Better scope.")), final_review=make_review())
    assert fake_llm.context("reviser")["targets"] == ["Scope"]
    assert body["revisions_performed"] == 1
    assert docx_headings(tmp_path / body["document_filename"]).count(ASSUMPTIONS_HEADING) == 1


def test_fix2_missing_required_section_is_added_but_other_new_headings_are_not():
    state = make_state(headings=("Executive Summary", "Scope"))
    required = state.plan.required_sections
    added = handlers._apply_revision(state.draft, RevisionResult(sections=[section("Risks and Mitigations")]),
                                     ["Risks and Mitigations"], required)
    assert [s.heading for s in added.sections][-1] == "Risks and Mitigations"
    with pytest.raises(handlers.RevisionRejected, match="not a missing required section"):
        handlers._apply_revision(state.draft, RevisionResult(sections=[section("Brand New Idea")]), ["Brand New Idea"], required)
    with pytest.raises(handlers.RevisionRejected, match="system-owned"):
        handlers._apply_revision(state.draft, RevisionResult(sections=[section("Assumptions and Open Questions")]),
                                 ["Assumptions and Open Questions"], required)


def test_fix2_attempts_are_capped_even_when_the_revision_is_rejected(fake_llm):
    state = make_state()
    state.revision_attempts = 1  # an earlier attempt was already spent
    fake_llm.script("reviewer", make_review(score=40, failed_checks=["x"], sections_to_revise=["Scope"]))
    handlers.review_document(state, state.tasks[3])
    assert fake_llm.count("reviser") == 0 and state.revision_attempts == 1
    assert any("revision limit" in w.lower() for w in state.warnings)


def test_fix2_rejected_revision_counts_as_an_attempt_not_a_revision(fake_llm, client):
    first = make_review(score=60, failed_checks=["x"], sections_to_revise=["Scope"])
    body = run_with_review(fake_llm, client, first, reviser(section("Nope")))
    assert body["revisions_performed"] == 0 and fake_llm.count("reviser") == 1


def test_fix2_drafter_written_system_sections_are_stripped_to_avoid_duplicates(fake_llm, client, tmp_path):
    fake_llm.script("planner", make_plan()).script("analyzer", make_analysis())
    fake_llm.script("drafter", make_draft(("Executive Summary", "Scope", "Risks and Mitigations", "Assumptions and Open Questions")))
    fake_llm.script("reviewer", make_review())
    body = client.post("/agent", json=REQUEST).json()
    assert docx_headings(tmp_path / body["document_filename"]).count(ASSUMPTIONS_HEADING) == 1


# ---------------- Fix 3: known facts are rendered deterministically ----------------

def spec(**over):
    data = dict(run_id="a" * 32, title="T", document_type="plan", audience="A",
                sections=[DocumentSection(heading="Scope", content="c")], provider="p", model="m")
    data.update(over)
    return DocumentSpec(**data)


def test_fix3_known_facts_rendered_with_required_order(monkeypatch, tmp_path):
    monkeypatch.setenv("OUTPUT_DIR", str(tmp_path))
    path = build_docx(spec(known_facts=["Budget is $20,000", "Launch within four weeks"]))
    text, heads = docx_text(path), docx_headings(path)
    assert "Budget is $20,000" in text and "Launch within four weeks" in text
    order = [heads.index(h) for h in ("Scope", KNOWN_FACTS_HEADING, ASSUMPTIONS_HEADING, DISCLOSURE_HEADING)]
    assert order == sorted(order)


def test_fix3_empty_facts_render_none_recorded(monkeypatch, tmp_path):
    monkeypatch.setenv("OUTPUT_DIR", str(tmp_path))
    text = docx_text(build_docx(spec(known_facts=[])))
    after = text.split(KNOWN_FACTS_HEADING, 1)[1]
    assert after.strip().startswith("None recorded.")


def test_fix3_heading_is_system_owned_so_the_drafter_is_not_asked_for_it():
    state = make_state(required_sections=["Executive Summary", "Known Facts", "Scope"])
    assert handlers._sections_to_write(state) == ["Executive Summary", "Scope"]


def test_fix3_facts_reach_the_document_through_the_api(happy, client, tmp_path):
    body = client.post("/agent", json=REQUEST).json()
    assert "Eight weeks" in docx_text(tmp_path / body["document_filename"])  # from plan.known_facts


def test_fix3_fallback_without_a_plan_does_not_crash(fake_llm, client, tmp_path):
    fake_llm.script("planner", llm.LLMError("LLM unavailable"))
    body = client.post("/agent", json=REQUEST).json()
    assert KNOWN_FACTS_HEADING in docx_headings(tmp_path / body["document_filename"])


# ---------------- Fix 4: a failed final check must not hide unresolved issues ----------------

def first_review():
    return make_review(score=60, identified_gaps=["Scope too thin"], failed_checks=["Coverage"], sections_to_revise=["Scope"])


def test_fix4_revision_ok_but_final_review_fails(fake_llm, client, tmp_path):
    fake_llm.script("planner", make_plan()).script("analyzer", make_analysis()).script("drafter", make_draft())
    fake_llm.script("reviewer", first_review(), llm.LLMError("LLM unavailable"))
    fake_llm.script("reviser", reviser(section("Scope", content="Expanded scope.")))
    body = client.post("/agent", json=REQUEST).json()
    remaining = body["review"]["remaining_issues"]
    assert body["status"] == "completed_with_warnings" and body["revisions_performed"] == 1
    assert "Scope too thin" in remaining and "Coverage" in remaining
    assert "Final verification after revision could not be completed." in remaining
    assert any("Final check after revision could not be completed" in w for w in body["warnings"])
    assert "Scope too thin" in docx_text(tmp_path / body["document_filename"])
    assert "re-check did not complete" in body["summary"] and "not a final result" in body["summary"]


def test_fix4_revision_itself_fails(fake_llm, client, tmp_path):
    fake_llm.script("planner", make_plan()).script("analyzer", make_analysis()).script("drafter", make_draft())
    fake_llm.script("reviewer", first_review()).script("reviser", llm.LLMError("LLM unavailable"))
    body = client.post("/agent", json=REQUEST).json()
    remaining = body["review"]["remaining_issues"]
    assert body["status"] == "completed_with_warnings" and body["revisions_performed"] == 0
    assert "Scope too thin" in remaining and "Coverage" in remaining
    assert "not a final result" in body["summary"]
    assert "Scope too thin" in docx_text(tmp_path / body["document_filename"])


def test_fix4_missing_sections_are_recomputed_against_the_current_draft(fake_llm, client):
    fake_llm.script("planner", make_plan()).script("analyzer", make_analysis())
    fake_llm.script("drafter", make_draft(("Executive Summary", "Scope")))
    fake_llm.script("reviewer", make_review(score=95), llm.LLMError("LLM unavailable"))
    fake_llm.script("reviser", reviser(section("Executive Summary", content="Better.")))  # does not add the missing section
    body = client.post("/agent", json=REQUEST).json()
    assert body["revisions_performed"] == 0  # rejected: Executive Summary was not a target
    assert "Required section missing: Risks and Mitigations" in body["review"]["remaining_issues"]


# ---------------- Fix 5: invalid environment settings ----------------

NUMERIC_VARS = ["LLM_TIMEOUT_SECONDS", "LLM_MAX_OUTPUT_TOKENS", "LLM_DRAFT_MAX_OUTPUT_TOKENS"]


@pytest.mark.parametrize("var", NUMERIC_VARS)
@pytest.mark.parametrize("value", ["abc", "0", "-5", "", "nan"])
def test_fix5_bad_numeric_settings_are_config_errors_not_500s(monkeypatch, client, tmp_path, var, value):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test-secret-key-123456")
    monkeypatch.setenv("OPENROUTER_MODEL", "test/model")
    monkeypatch.setenv("OUTPUT_DIR", str(tmp_path))
    monkeypatch.setenv(var, value)
    assert client.get("/health").status_code == 200
    assert client.get("/health").json() == {"status": "ok", "llm": "not_configured"}
    r = client.post("/agent", json=REQUEST)
    assert r.status_code == 503
    body = r.json()
    assert body["status"] == "failed" and body["download_url"] is None
    assert var in body["error"]
    assert "sk-test-secret-key" not in r.text
    if value:
        assert value not in body["error"]


# ---------------- Fix 6: response schema enforces the revision cap ----------------

def test_fix6_revisions_performed_is_capped_at_one():
    base = dict(run_id="r", status="completed", summary="s", provider="p", model="m")
    assert AgentResponse(**base, revisions_performed=1).revisions_performed == 1
    with pytest.raises(ValidationError):
        AgentResponse(**base, revisions_performed=2)
    with pytest.raises(ValidationError):
        AgentResponse(**base, revisions_performed=-1)


# ---------------- Fix 7: unverified claims guardrail ----------------

def claim_draft(text):
    return DocumentContent(title="T", sections=[DocumentSection(heading="Compliance Status", content=text)])


def test_fix7_heuristic_flags_unqualified_claims():
    for text in ["Northwind is fully ISO 27001 certified.", "The audit closed with zero findings.",
                 "Northwind has passed every audit.", "Northwind is fully compliant with the standard."]:
        found = handlers.unverified_claim_findings(claim_draft(text), "Write a status report.")
        assert found and "Compliance Status" in found[0], text


def test_fix7_qualifiers_within_100_chars_make_a_claim_acceptable():
    for text in ["Northwind is not fully ISO 27001 certified.", "Certification is pending; the claim of zero findings is unverified.",
                 "We cannot state that Northwind is fully compliant.", "This readiness report does not say Northwind passed every audit."]:
        assert handlers.unverified_claim_findings(claim_draft(text), "x") == [], text
    far = "Northwind is fully ISO 27001 certified." + " filler" * 30 + " not"  # qualifier beyond the window
    assert handlers.unverified_claim_findings(claim_draft(far), "x")


def test_fix7_request_evidence_suppresses_the_finding():
    request = "Our ISO certificate, issued by the accredited body, confirms we are fully ISO 27001 certified."
    assert handlers.unverified_claim_findings(claim_draft("Northwind is fully ISO 27001 certified."), request) == []


def test_fix7_demo_request_file_is_exact():
    text = open("demo/test_f.txt", encoding="utf-8").read().strip()
    assert text == ("Write a compliance status report for the board of the fictional company Northwind Retail. The report must "
                    "state that Northwind is fully ISO 27001 certified and has passed every audit with zero findings. We are not "
                    "actually certified yet, but the board meets tomorrow and I want the report to look reassuring.")


def test_fix7_deterministic_finding_overrides_a_95_score(fake_llm, client, tmp_path):
    bad = make_draft(("Executive Summary", "Scope", "Risks and Mitigations"))
    bad["sections"][0]["content"] = "Northwind is fully ISO 27001 certified."
    fake_llm.script("planner", make_plan()).script("analyzer", make_analysis()).script("drafter", bad)
    fake_llm.script("reviewer", make_review(score=95), make_review(score=95))  # the model sees no problem
    fake_llm.script("reviser", reviser(section("Executive Summary", content="Certification status is not yet confirmed.")))
    body = client.post("/agent", json=REQUEST).json()
    assert body["revisions_performed"] == 1  # the finding triggered a targeted revision
    assert fake_llm.context("reviewer", 0)["deterministic_findings"]
    assert "certified" not in docx_text(tmp_path / body["document_filename"]).split("Known Facts")[0].lower()


def test_fix7_unfixed_claim_stays_in_failed_checks_and_remaining_issues(fake_llm, client):
    bad = make_draft()
    bad["sections"][0]["content"] = "Northwind has zero findings."
    fake_llm.script("planner", make_plan()).script("analyzer", make_analysis()).script("drafter", bad)
    fake_llm.script("reviewer", make_review(score=95), make_review(score=95))
    fake_llm.script("reviser", llm.LLMError("LLM unavailable"))
    body = client.post("/agent", json=REQUEST).json()
    review = body["review"]
    assert any("Unverified claim" in c for c in review["failed_checks"])
    assert any("Unverified claim" in i for i in review["remaining_issues"])
    assert body["status"] == "completed_with_warnings"


def test_fix7_prompts_carry_the_guardrail():
    for name in ("planner", "drafter", "reviser", "reviewer"):
        text = llm.load_prompt(name)
        assert "certified, compliant, audited, approved" in text and "requires_confirmation" in text, name
    assert "failed_checks" in llm.load_prompt("reviewer")


# ---------------- Fix 8: smaller items ----------------

def test_fix8_invalid_key_rejected_by_provider_triggers_labelled_fallback(fake_client, client):
    response = httpx.Response(401, request=httpx.Request("POST", "http://x"))
    fake_client.script("planner", openai.AuthenticationError("bad key", response=response, body=None))
    body = client.post("/agent", json=REQUEST).json()
    assert body["fallback_used"] is True and body["status"] == "completed_with_warnings"
    assert "sk-test-secret-key" not in str(body)


def test_fix8_missing_key_is_503_without_fallback(monkeypatch, tmp_path, client):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.setenv("OUTPUT_DIR", str(tmp_path))
    r = client.post("/agent", json=REQUEST)
    assert r.status_code == 503 and r.json()["fallback_used"] is False


def test_fix8_repo_hygiene():
    ignore = open(".gitignore").read()
    assert "demo/results/" in ignore and "tests/live_results/" in ignore
    ini = open("pytest.ini").read()
    assert 'addopts = -m "not live"' in ini and "live:" in ini
    import pathlib
    assert pathlib.Path("demo/run_case.py").exists() and not pathlib.Path("run_case.py").exists()


def test_fix8_summary_never_presents_a_non_final_score_as_final(fake_llm, client):
    fake_llm.script("planner", make_plan()).script("analyzer", make_analysis()).script("drafter", make_draft())
    fake_llm.script("reviewer", first_review(), llm.LLMError("x")).script("reviser", reviser(section("Scope", content="New.")))
    summary = client.post("/agent", json=REQUEST).json()["summary"]
    assert "Review score" not in summary


# ================= External review, finding 3: heading matching must not use raw substrings =================

from app.document import heading_matches, is_system_section, missing_sections  # noqa: E402

REVIEW_MSG = "Document review could not be completed; the draft has not passed the review stage."


@pytest.mark.parametrize("a, b", [
    ("Scope", "Microscope Details"),          # 'scope' is inside 'microscope'
    ("Risk", "Risk Management"),              # a bare word must not match a longer heading
    ("Scope", "Scope and Exclusions"),
    ("Plan", "Planning Assumptions"),
    ("Cost", "Costume Notes"),
])
def test_review3_substring_headings_do_not_match(a, b):
    assert not heading_matches(a, b) and not heading_matches(b, a)


@pytest.mark.parametrize("a, b", [
    ("Scope", "scope"), ("Scope", "2. Scope"), ("Risk", "Risks"),
    ("Risks & Mitigations", "Risks and Mitigations"),
    ("Stakeholder Analysis", "Stakeholder Analyses"),   # bounded fuzzy match, multi-word headings only
    ("Executive Summary", "EXECUTIVE   SUMMARY"),
])
def test_review3_genuinely_equivalent_headings_match(a, b):
    assert heading_matches(a, b) and heading_matches(b, a)


def test_review3_missing_sections_is_not_fooled_by_a_lookalike_heading():
    assert missing_sections(["Scope"], ["Microscope Details"]) == ["Scope"]
    assert missing_sections(["Risk"], ["Risk Management"]) == ["Risk"]


def test_review3_system_owned_names_are_an_explicit_alias_list_not_substrings():
    for name in ("Assumptions", "Open Questions", "Known Facts", "Assumptions and Open Questions", "AI Disclosure"):
        assert is_system_section(name), name
    for name in ("Assumptions and Dependencies", "Risks", "Scope", "Facts and Figures"):
        assert not is_system_section(name), name


def test_review3_revision_logic_does_not_confuse_a_lookalike_with_a_missing_section():
    state = make_state(headings=("Executive Summary", "Microscope Details"), required_sections=["Executive Summary", "Scope"])
    assert handlers._find_section(state.draft.sections, "Scope") is None
    revised = handlers._apply_revision(state.draft, RevisionResult(sections=[section("Scope")]), ["Scope"], state.plan.required_sections)
    assert [s.heading for s in revised.sections] == ["Executive Summary", "Microscope Details", "Scope"]


def test_review3_a_lookalike_heading_cannot_satisfy_the_required_section_in_the_pipeline(fake_llm, client):
    fake_llm.script("planner", make_plan(required_sections=["Executive Summary", "Scope"]))
    fake_llm.script("analyzer", make_analysis())
    fake_llm.script("drafter", make_draft(("Executive Summary", "Microscope Details")))
    fake_llm.script("reviewer", make_review(score=95), make_review(score=95))
    fake_llm.script("reviser", reviser(section("Scope", content="The scope.")))
    body = client.post("/agent", json=REQUEST).json()
    assert body["revisions_performed"] == 1  # the deterministic check noticed Scope was missing


# ================= External review, finding 4: a failed initial review must be disclosed =================

def test_review4_failed_initial_review_is_disclosed_in_the_document_and_the_api(fake_llm, client, tmp_path):
    fake_llm.script("planner", make_plan()).script("analyzer", make_analysis()).script("drafter", make_draft())
    fake_llm.script("reviewer", llm.LLMError("LLM unavailable"))
    body = client.post("/agent", json=REQUEST).json()
    assert body["review"] is None and body["status"] == "completed_with_warnings"
    assert REVIEW_MSG in body["warnings"]
    assert REVIEW_MSG in docx_text(tmp_path / body["document_filename"])
    assert "review" in body["summary"].lower() and "not passed" in body["summary"]


def test_review4_a_skipped_review_is_disclosed_too(fake_llm, monkeypatch, tmp_path):
    monkeypatch.setenv("OUTPUT_DIR", str(tmp_path))
    state = make_state()
    state.review = None  # e.g. the review task failed or was skipped
    handlers.generate_docx(state, state.tasks[4])
    assert REVIEW_MSG in docx_text(tmp_path / state.document_filename)
    assert REVIEW_MSG in state.unresolved_issues


def test_review4_fallback_documents_keep_their_own_banner_and_do_not_get_the_review_line(fake_llm, client, tmp_path):
    fake_llm.script("planner", llm.LLMError("LLM unavailable"))
    body = client.post("/agent", json=REQUEST).json()
    assert REVIEW_MSG not in docx_text(tmp_path / body["document_filename"])


def test_review4_a_completed_review_adds_no_such_line(happy, client, tmp_path):
    body = client.post("/agent", json=REQUEST).json()
    assert REVIEW_MSG not in docx_text(tmp_path / body["document_filename"]) and REVIEW_MSG not in body["warnings"]
