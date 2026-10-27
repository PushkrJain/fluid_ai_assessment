"""Agent workflow: shared run state, runtime planning, controlled sequential execution,
labelled fallback, and response assembly."""
import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from . import llm
from .handlers import HANDLERS
from .schemas import (
    AgentResponse,
    Assumption,
    DocumentContent,
    DocumentSection,
    Plan,
    ReviewReport,
    Task,
)

logger = logging.getLogger("agent.run")

# If one of these fails the deliverable is at risk; the others only reduce quality.
NONCRITICAL = {"analyze_request", "resolve_assumptions", "review_document"}
LOW_SCORE = 70
STAGE_NAMES = {
    "analyze_request": "analysis",
    "resolve_assumptions": "assumption resolution",
    "draft_document": "drafting",
    "review_document": "review",
    "generate_docx": "document generation",
}
PLANNING_FAILED_NOTICE = "No AI planning, analysis, drafting or review was completed for this run."


@dataclass
class AgentState:
    """Single source of truth for one request. Lives in memory for the duration of the run."""

    request: str
    provider: str
    model: str
    run_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    plan: Plan | None = None
    tasks: list[Task] = field(default_factory=list)
    assumptions: list[Assumption] = field(default_factory=list)
    open_questions: list[str] = field(default_factory=list)
    analysis_notes: list[dict[str, Any]] = field(default_factory=list)
    draft: DocumentContent | None = None
    review: ReviewReport | None = None
    review_history: list[ReviewReport] = field(default_factory=list)
    revision_log: list[str] = field(default_factory=list)
    revisions_performed: int = 0  # ACCEPTED revisions only
    revision_attempts: int = 0  # every attempt counts toward the cap, accepted or not
    review_finalized: bool = False  # False when a post-revision re-check did not complete
    fallback_used: bool = False
    fallback_notice: str | None = None
    warnings: list[str] = field(default_factory=list)
    unresolved_issues: list[str] = field(default_factory=list)  # printed in the document AND returned as warnings
    document_filename: str | None = None
    document_verified: bool = False
    error: str | None = None
    timings: dict[int, float] = field(default_factory=dict)

    def plan_context(self) -> dict[str, Any]:
        if self.plan is None:
            return {}
        return {
            "document_type": self.plan.document_type,
            "title": self.plan.title,
            "audience": self.plan.audience,
            "known_facts": self.plan.known_facts,
            "requirements": self.plan.requirements,
            "constraints": self.plan.constraints,
            "assumptions": [a.model_dump() for a in self.assumptions],
            "open_questions": self.open_questions,
            "required_sections": self.plan.required_sections,
        }


def _log(state: AgentState, event: str, **fields: object) -> None:
    extras = " ".join(f"{k}={v}" for k, v in fields.items())
    logger.info("%s run_id=%s %s", event, state.run_id, extras)


# ---------- planning ----------

def create_plan(state: AgentState) -> None:
    started = time.monotonic()
    message = f"<context>\n{json.dumps({'request': state.request}, ensure_ascii=False)}\n</context>"
    plan = llm.generate_structured(llm.load_prompt("planner"), message, Plan)
    state.plan = plan
    state.tasks = [Task.from_planned(t) for t in plan.tasks]
    state.assumptions = list(plan.assumptions)
    state.open_questions = list(plan.open_questions)
    _log(state, "plan_created", tasks=len(plan.tasks), sections=len(plan.required_sections),
         seconds=f"{time.monotonic() - started:.1f}")


# ---------- fallback (always labelled; wording derived from what actually completed) ----------

def completed_stages(state: AgentState) -> list[str]:
    stages = ["planning"] if state.plan is not None else []
    for task in state.tasks:
        name = STAGE_NAMES.get(task.handler)
        if task.status == "completed" and name and name not in stages:
            stages.append(name)
    return stages


def _fallback_notice(state: AgentState, failed_stage: str) -> str:
    if state.plan is None:
        return PLANNING_FAILED_NOTICE
    done = ", ".join(completed_stages(state)) or "none"
    return f"AI-generated content could not be completed. Completed stages: {done}. Failed stage: {failed_stage}."


def _fallback_draft(state: AgentState) -> DocumentContent:
    sections = [
        DocumentSection(heading="Notice", content=state.fallback_notice or ""),
        DocumentSection(heading="Original Request", content=state.request),
    ]
    if state.plan:
        sections.append(DocumentSection(
            heading="Planned Structure (not drafted)",
            content="The planning step succeeded, but the content below was never written.",
            bullets=state.plan.required_sections,
        ))
    sections.append(DocumentSection(
        heading="Next Steps",
        content="Check the LLM provider configuration and availability, then submit the request again.",
    ))
    title = state.plan.title if state.plan else "Request Summary (AI Generation Unavailable)"
    return DocumentContent(title=title, sections=sections)


def _enter_fallback(state: AgentState, reason: str, failed_stage: str) -> None:
    state.fallback_used = True
    state.fallback_notice = _fallback_notice(state, failed_stage)
    state.draft = _fallback_draft(state)
    state.warnings.append(f"Fallback document used: {reason}")
    _log(state, "fallback_activated", failed_stage=failed_stage)


# ---------- execution ----------

def _dependencies_ok(task: Task, by_id: dict[int, Task]) -> bool:
    for dep_id in task.dependencies:
        dep = by_id[dep_id]
        if dep.status == "completed":
            continue
        if dep.status == "failed" and dep.handler in NONCRITICAL:
            continue  # quality step failed, but the pipeline may continue
        return False
    return True


def _safe_error(exc: Exception) -> str:
    return str(exc) if isinstance(exc, llm.LLMError) else f"Unexpected {type(exc).__name__}"


def _run_task(state: AgentState, task: Task) -> None:
    task.status = "running"
    _log(state, "task_running", task=task.id, handler=task.handler)
    started = time.monotonic()
    try:
        task.result_summary = HANDLERS[task.handler](state, task)  # allowlist lookup only
        task.status = "completed"
    except Exception as exc:
        task.status = "failed"
        task.error = _safe_error(exc)
        if not isinstance(exc, llm.LLMError):
            logger.exception("task_unexpected_error run_id=%s task=%s", state.run_id, task.id)
    state.timings[task.id] = time.monotonic() - started
    _log(state, "task_finished", task=task.id, handler=task.handler, status=task.status,
         seconds=f"{state.timings[task.id]:.1f}")


def _skip(state: AgentState, task: Task, reason: str) -> None:
    task.status = "skipped"
    task.result_summary = reason
    _log(state, "task_skipped", task=task.id, handler=task.handler, reason=reason)


def execute_plan(state: AgentState, *, fallback_mode: bool = False) -> None:
    by_id = {t.id: t for t in state.tasks}
    for task in state.tasks:
        if fallback_mode and task.handler != "generate_docx":
            _skip(state, task, "Skipped: fallback document in use")
            continue
        if not fallback_mode and not _dependencies_ok(task, by_id):
            _skip(state, task, "Skipped: a dependency did not complete")
            continue

        _run_task(state, task)

        if task.status == "failed":
            if task.handler == "draft_document":
                _enter_fallback(state, f"drafting failed ({task.error})", "drafting")
                fallback_mode = True
            elif task.handler == "generate_docx":
                state.error = task.error
            elif task.handler in NONCRITICAL:
                state.warnings.append(f"Task {task.id} ({task.handler}) failed: {task.error}")


def _planning_fallback_tasks() -> list[Task]:
    return [Task(
        id=1, title="Generate fallback notice document",
        description="Planning was unavailable; write a labelled template document.",
        handler="generate_docx", expected_output="Verified .docx notice",
    )]


# ---------- response ----------

def _status(state: AgentState) -> str:
    if not (state.document_verified and state.document_filename):
        return "failed"
    review = state.review
    needs_warning = (
        state.fallback_used
        or bool(state.warnings)
        or any(t.status != "completed" for t in state.tasks)
        or review is None
        or bool(review.remaining_issues)
        or review.score < LOW_SCORE
    )
    return "completed_with_warnings" if needs_warning else "completed"


def _summary(state: AgentState, status: str) -> str:
    if status == "failed":
        return "No valid document could be produced."
    if state.fallback_used:
        return f"{state.fallback_notice} A clearly labelled fallback document was generated."
    plan = state.plan
    done = sum(t.status == "completed" for t in state.tasks)
    parts = [f"Generated a {plan.document_type if plan else 'document'} ('{plan.title if plan else ''}') "
             f"for {plan.audience if plan else 'the requester'}.",
             f"{done}/{len(state.tasks)} planned tasks completed;"
             f" {state.revisions_performed} revision(s) performed."]
    if state.review:
        remaining = len(state.review.remaining_issues)
        if state.review_finalized:
            parts.append(f"Review score {state.review.score}/100 with {remaining} remaining issue(s).")
        else:
            parts.append(f"The final re-check did not complete; the initial review score of {state.review.score}/100 "
                         f"is not a final result. {remaining} issue(s) remain unresolved.")
    else:
        parts.append("The review step did not complete; the draft has not passed the review stage.")
    if state.open_questions:
        parts.append(f"{len(state.open_questions)} open question(s) need human confirmation.")
    return " ".join(parts)


def build_response(state: AgentState) -> AgentResponse:
    status = _status(state)
    ok = status != "failed"
    return AgentResponse(
        run_id=state.run_id,
        status=status,
        summary=_summary(state, status),
        document_type=state.plan.document_type if state.plan else None,
        plan=state.tasks,
        assumptions=state.assumptions,
        open_questions=state.open_questions,
        review=state.review,
        revisions_performed=state.revisions_performed,
        fallback_used=state.fallback_used,
        warnings=state.warnings + [i for i in state.unresolved_issues if i not in state.warnings],
        provider=state.provider,
        model=state.model,
        document_filename=state.document_filename if ok else None,
        download_url=f"/documents/{state.document_filename}" if ok else None,
        error=None if ok else (state.error or "Document generation failed"),
    )


def run_agent(request_text: str) -> AgentResponse:
    provider, model = llm.provider_info()
    state = AgentState(request=request_text, provider=provider, model=model)
    _log(state, "run_started", request_chars=len(request_text))  # never log request content

    try:
        create_plan(state)
        execute_plan(state)
    except llm.LLMConfigError as exc:
        state.error = f"LLM is not configured: {exc}"
        _log(state, "run_failed", reason="configuration")
    except llm.LLMError as exc:  # planning failed: labelled fallback
        _enter_fallback(state, f"planning failed ({exc})", "planning")
        state.tasks = _planning_fallback_tasks()
        execute_plan(state, fallback_mode=True)

    response = build_response(state)
    _log(state, "run_finished", status=response.status, fallback=state.fallback_used,
         revisions=state.revisions_performed)
    return response
