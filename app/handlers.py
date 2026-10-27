"""Allowlisted task handlers. The executor can only run functions registered in HANDLERS;
model output selects a handler NAME, never code."""
from __future__ import annotations

import json
import logging
import re
from typing import TYPE_CHECKING, Callable

from .document import (
    SYSTEM_HEADINGS,
    DocumentSpec,
    build_docx,
    heading_matches,
    is_system_section,
    missing_sections,
    normalize_heading,
    verify_docx,
)
from .llm import LLMError, generate_structured, get_settings, load_prompt, provider_info
from .schemas import (
    AnalysisNote,
    Assumption,
    DocumentContent,
    DocumentSection,
    ReviewReport,
    RevisionResult,
    Task,
)

if TYPE_CHECKING:  # avoid a circular import; agent.py imports this module
    from .agent import AgentState

logger = logging.getLogger("agent.handlers")

MAX_REVISIONS = 1  # reflection: at most one revision ATTEMPT, then a final check
MAX_REVISION_TARGETS = 6
LOW_SCORE = 70
REVIEW_NOT_DONE = "Document review could not be completed; the draft has not passed the review stage."
ANALYSIS_TOKENS = 3000
REVIEW_TOKENS = 4000

Handler = Callable[["AgentState", Task], str]


class RevisionRejected(ValueError):
    """The reviser's output touched something it was not allowed to touch."""


# ---------- unverified-claim heuristic ----------
# HEURISTIC, NOT A COMPLETE CONTROL. A short regex list plus a qualifier window catches the most
# common "certified / zero findings" overclaims, so the model's own score cannot wave them
# through. It will miss paraphrases and can flag harmless sentences. Human review is still required.
CLAIM_PATTERNS = (
    r"fully .{0,20}certified",
    r"zero findings",
    r"passed (?:all|every) audits?",
    r"fully compliant",
)
QUALIFIERS = re.compile(
    r"\b(?:not|no|unverified|cannot|pending|readiness|claims?|claimed)\b|n't|to be confirmed", re.IGNORECASE
)
EVIDENCE = re.compile(r"\b(?:certificate|certification body|accredited|audit report|auditor|attestation)\b", re.IGNORECASE)
CLAIM_WINDOW = 100


def unqualified_matches(text: str, pattern: str, *, qualifiers: re.Pattern = QUALIFIERS, window: int = CLAIM_WINDOW) -> list[str]:
    """Snippets where `pattern` matches with NO qualifier word within `window` characters."""
    snippets = []
    for match in re.finditer(pattern, text, re.IGNORECASE):
        around = text[max(0, match.start() - window): match.end() + window]
        if not qualifiers.search(around):
            snippets.append(re.sub(r"\s+", " ", text[max(0, match.start() - 30): match.end() + 30]).strip())
    return snippets


def _request_gives_evidence(request: str, pattern: str) -> bool:
    for match in re.finditer(pattern, request, re.IGNORECASE):
        around = request[max(0, match.start() - CLAIM_WINDOW): match.end() + CLAIM_WINDOW]
        if EVIDENCE.search(around) and not QUALIFIERS.search(around):
            return True
    return False


def _section_text(section: DocumentSection) -> str:
    parts = [section.heading, section.content, *section.bullets]
    if section.table:
        parts += section.table.columns + [cell for row in section.table.rows for cell in row]
    return "\n".join(parts)


def _scan_claims(draft: DocumentContent, request: str) -> list[tuple[str, str]]:
    found: list[tuple[str, str]] = []
    for section in draft.sections:
        text = _section_text(section)
        for pattern in CLAIM_PATTERNS:
            if _request_gives_evidence(request, pattern):
                continue
            for snippet in unqualified_matches(text, pattern):
                found.append((section.heading, f"Unverified claim in section '{section.heading}': '{snippet[:120]}'"))
    return found


def unverified_claim_findings(draft: DocumentContent, request: str) -> list[str]:
    return [message for _, message in _scan_claims(draft, request)]


# ---------- helpers ----------

def _dump(data: object) -> str:
    return json.dumps(data, ensure_ascii=False, indent=1)


def _context(state: AgentState, **extra: object) -> str:
    payload = {"request": state.request, **state.plan_context(), **extra}
    return f"<context>\n{_dump(payload)}\n</context>"


def _headings(draft: DocumentContent) -> list[str]:
    return [s.heading for s in draft.sections] + list(SYSTEM_HEADINGS)


def _required(state: AgentState) -> list[str]:
    return state.plan.required_sections if state.plan else []


def _sections_to_write(state: AgentState) -> list[str]:
    return [name for name in _required(state) if not is_system_section(name)]


def _add_unique(items: list[str], new: list[str]) -> None:
    existing = {i.strip().lower() for i in items}
    for item in new:
        if item.strip() and item.strip().lower() not in existing:
            items.append(item.strip())
            existing.add(item.strip().lower())


def _strip_system_sections(draft: DocumentContent) -> DocumentContent:
    """The system renders facts, assumptions and disclosure itself; drop copies the model wrote
    so the saved document never has duplicate headings."""
    kept = [s for s in draft.sections if not is_system_section(s.heading)]
    if not kept or len(kept) == len(draft.sections):
        return draft
    logger.info("system_sections_stripped count=%d", len(draft.sections) - len(kept))
    return DocumentContent(title=draft.title, sections=kept)


# ---------- handlers ----------

def analyze_request(state: AgentState, task: Task) -> str:
    """Model-backed analysis that follows THIS task's description from the runtime plan."""
    user = _context(
        state,
        task={"title": task.title, "description": task.description, "expected_output": task.expected_output},
        earlier_analysis=state.analysis_notes,
    )
    note = generate_structured(load_prompt("analyzer"), user, AnalysisNote, max_output_tokens=ANALYSIS_TOKENS)
    state.analysis_notes.append({"task_id": task.id, "task": task.title, **note.model_dump()})
    return note.summary[:300]


def resolve_assumptions(state: AgentState, task: Task) -> str:
    """Deterministic: fold analysis conflicts and questions into the assumptions register."""
    known = {a.statement.strip().lower() for a in state.assumptions}
    for note in state.analysis_notes:
        for conflict in note.get("conflicts", []):
            if conflict.strip().lower() not in known:
                state.assumptions.append(Assumption(
                    statement=conflict.strip(), category="constraint_conflict",
                    impact="Identified during analysis; may require trade-off decisions.",
                ))
                known.add(conflict.strip().lower())
        _add_unique(state.open_questions, note.get("open_questions", []))
    to_confirm = sum(a.category in ("requires_confirmation", "constraint_conflict") for a in state.assumptions)
    return f"Register holds {len(state.assumptions)} items ({to_confirm} need confirmation); {len(state.open_questions)} open questions"


def draft_document(state: AgentState, task: Task) -> str:
    user = _context(
        state,
        sections_to_write=_sections_to_write(state),
        analysis_notes=state.analysis_notes,
        task={"title": task.title, "description": task.description},
    )
    draft = generate_structured(
        load_prompt("drafter"), user, DocumentContent, max_output_tokens=get_settings().draft_max_output_tokens
    )
    state.draft = _strip_system_sections(draft)
    return f"Drafted {len(state.draft.sections)} sections"


# ---------- reflection ----------

def _merge_deterministic(report: ReviewReport, missing: list[str], final: bool) -> ReviewReport:
    """Deterministic heading check complements the model's self-review."""
    for name in missing:
        message = f"Required section missing: {name}"
        if message not in report.failed_checks:
            report.failed_checks.append(message)
        if not any(heading_matches(name, s) for s in report.sections_to_revise):
            report.sections_to_revise.append(name)
        if final and message not in report.remaining_issues:
            report.remaining_issues.append(message)
    return report


def _merge_claims(report: ReviewReport, claims: list[tuple[str, str]]) -> ReviewReport:
    for heading, message in claims:
        if message not in report.failed_checks:
            report.failed_checks.append(message)
        if message not in report.remaining_issues:
            report.remaining_issues.append(message)
        if not any(heading_matches(heading, s) for s in report.sections_to_revise):
            report.sections_to_revise.append(heading)
    return report


def _review(state: AgentState, final: bool) -> ReviewReport:
    assert state.draft is not None
    missing = missing_sections(_required(state), _headings(state.draft))
    claims = _scan_claims(state.draft, state.request)
    user = _context(
        state,
        draft=state.draft.model_dump(),
        deterministic_findings=[f"Required section missing: {m}" for m in missing] + [c[1] for c in claims],
    )
    report = generate_structured(load_prompt("reviewer"), user, ReviewReport, max_output_tokens=REVIEW_TOKENS)
    return _merge_claims(_merge_deterministic(report, missing, final), claims)


def _revision_targets(state: AgentState, report: ReviewReport) -> list[str]:
    assert state.draft is not None
    targets: list[str] = []
    for name in report.sections_to_revise:
        if name.strip() and not is_system_section(name) and not any(heading_matches(name, t) for t in targets):
            targets.append(name.strip())
    if not targets and report.failed_checks and report.score < LOW_SCORE:
        targets = [s.heading for s in state.draft.sections if not is_system_section(s.heading)]
    return targets[:MAX_REVISION_TARGETS]


def _find_section(sections: list[DocumentSection], heading: str) -> int | None:
    norm = normalize_heading(heading)
    for i, section in enumerate(sections):
        if normalize_heading(section.heading) == norm:
            return i
    return next((i for i, s in enumerate(sections) if heading_matches(heading, s.heading)), None)


def _similar_pairs(draft: DocumentContent) -> set[tuple[str, str]]:
    heads = [s.heading for s in draft.sections]
    return {
        tuple(sorted((normalize_heading(a), normalize_heading(b))))
        for i, a in enumerate(heads) for b in heads[i + 1:] if heading_matches(a, b)
    }


def _validate_revised(original: DocumentContent, revised: DocumentContent, touched: set[int]) -> None:
    """Every section the reviser was not approved to touch must be byte-for-byte unchanged."""
    if revised.title != original.title:
        raise RevisionRejected("the title was changed")
    for index, old in enumerate(original.sections):
        if index not in touched and (index >= len(revised.sections) or revised.sections[index] != old):
            raise RevisionRejected(f"section '{old.heading[:60]}' is not an approved target but changed")
    if _similar_pairs(revised) - _similar_pairs(original):
        raise RevisionRejected("the revision introduced duplicate or near-duplicate headings")


def _apply_revision(
    draft: DocumentContent, revision: RevisionResult, targets: list[str], required_sections: list[str]
) -> DocumentContent:
    sections = list(draft.sections)
    touched: set[int] = set()
    missing = missing_sections(required_sections, [s.heading for s in sections] + list(SYSTEM_HEADINGS))
    for new in revision.sections:
        label = new.heading[:60]
        if is_system_section(new.heading):
            raise RevisionRejected(f"returned a system-owned section ('{label}')")
        target = next((t for t in targets if heading_matches(new.heading, t)), None)
        if target is None:
            raise RevisionRejected(f"returned section '{label}' is not an approved target")
        index = _find_section(sections, new.heading)
        if index is not None:
            if index in touched or not heading_matches(sections[index].heading, target):
                raise RevisionRejected(f"returned section '{label}' does not map cleanly to one target")
            sections[index] = new
            touched.add(index)
        elif any(heading_matches(target, m) for m in missing):
            sections.append(new)
            touched.add(len(sections) - 1)
        else:
            raise RevisionRejected(f"'{label}' is not a missing required section, so it cannot be added")
    result = DocumentContent(title=draft.title, sections=sections)
    _validate_revised(draft, result, touched)
    return result


def _attempt_revision(state: AgentState, report: ReviewReport, targets: list[str]) -> bool:
    """One revision ATTEMPT. Returns True only if the revision was accepted and applied."""
    assert state.draft is not None
    state.revision_attempts += 1
    user = _context(state, draft=state.draft.model_dump(), review=report.model_dump(), targets=targets)
    try:
        revision = generate_structured(
            load_prompt("reviser"), user, RevisionResult, max_output_tokens=get_settings().draft_max_output_tokens
        )
        revised = _apply_revision(state.draft, revision, targets, _required(state))
    except LLMError as exc:
        state.warnings.append(f"Revision could not be completed; original draft kept ({exc})")
        return False
    except RevisionRejected as exc:
        state.warnings.append(f"Revision rejected; original draft kept ({exc})")
        return False
    state.draft = revised
    state.revisions_performed += 1
    state.revision_log.append(revision.summary or f"Revised sections: {', '.join(targets)}")
    return True


def _carry_unresolved(state: AgentState, first: ReviewReport, closing_line: str) -> ReviewReport:
    """No trustworthy final report exists: keep EVERYTHING the first review found as unresolved."""
    assert state.draft is not None
    report = first.model_copy(deep=True)
    _add_unique(report.remaining_issues, report.identified_gaps + report.failed_checks)
    _merge_deterministic(report, missing_sections(_required(state), _headings(state.draft)), final=True)
    _add_unique(report.remaining_issues, [closing_line])
    return report


def review_document(state: AgentState, task: Task) -> str:
    """Reflection / self-check: review, at most ONE revision attempt, then a final check."""
    if state.draft is None:
        raise RuntimeError("No draft available to review")

    first = _review(state, final=False)
    state.review_history.append(first)
    state.review = first
    state.review_finalized = True  # stays True only if no revision is attempted or the re-check completes

    targets = _revision_targets(state, first)
    if targets and state.revision_attempts >= MAX_REVISIONS:
        state.warnings.append("Review found issues but the revision limit was reached")
    elif targets:
        state.review_finalized = False
        if _attempt_revision(state, first, targets):
            try:
                final = _review(state, final=True)
            except LLMError as exc:
                state.warnings.append(f"Final check after revision could not be completed ({exc})")
                state.review = _carry_unresolved(state, first, "Final verification after revision could not be completed.")
            else:
                state.review_history.append(final)
                state.review = final
                state.review_finalized = True
        else:
            state.review = _carry_unresolved(state, first, "Revision could not be applied; the original draft was kept.")

    report = state.review
    return (
        f"Score {report.score}{'' if state.review_finalized else ' (not final)'}; {len(state.review_history)} review(s); "
        f"{state.revisions_performed} revision(s); {len(report.remaining_issues)} remaining issue(s)"
    )


def generate_docx(state: AgentState, task: Task) -> str:
    if state.draft is None:
        raise RuntimeError("No draft available for document generation")
    plan = state.plan
    provider, model = provider_info()
    if state.review is None and not state.fallback_used:
        # The review failed or was skipped: the reader of the document must be told.
        _add_unique(state.unresolved_issues, [REVIEW_NOT_DONE])
    spec = DocumentSpec(
        run_id=state.run_id,
        title=state.draft.title or (plan.title if plan else "Request Summary"),
        document_type=plan.document_type if plan else "fallback notice",
        audience=plan.audience if plan else "Requester",
        sections=state.draft.sections,
        known_facts=list(plan.known_facts) if plan else [],
        assumptions=state.assumptions,
        open_questions=state.open_questions,
        remaining_issues=(list(state.review.remaining_issues) if state.review else []) + state.unresolved_issues,
        fallback_notice=state.fallback_notice,
        provider=provider,
        model=model,
    )
    path = build_docx(spec)
    check = verify_docx(path, spec.title, _required(state))
    if not check.ok:
        raise RuntimeError("Document verification failed: " + "; ".join(check.problems))
    if check.missing_sections:
        state.warnings.append("Required sections missing from the document: " + ", ".join(check.missing_sections))
    state.document_filename = path.name
    state.document_verified = True
    return f"Saved and verified {path.name} ({len(check.headings)} headings)"


HANDLERS: dict[str, Handler] = {
    "analyze_request": analyze_request,
    "resolve_assumptions": resolve_assumptions,
    "draft_document": draft_document,
    "review_document": review_document,
    "generate_docx": generate_docx,
}
