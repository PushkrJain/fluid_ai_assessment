"""Pydantic models: API request/response, LLM-generated plan, review report, document content."""
from typing import Annotated, Literal

from pydantic import BaseModel, Field, StringConstraints, field_validator, model_validator

TaskStatus = Literal["pending", "running", "completed", "failed", "skipped"]
HandlerName = Literal[
    "analyze_request",
    "resolve_assumptions",
    "draft_document",
    "review_document",
    "generate_docx",
]
# Handlers every plan must contain, in this order. The LLM chooses titles, descriptions,
# extra steps and wording; Python guarantees the pipeline can actually deliver a document.
REQUIRED_PIPELINE: tuple[str, ...] = ("draft_document", "review_document", "generate_docx")
MIN_TASKS, MAX_TASKS = 3, 7
# Max uses per handler in one plan (default 1). analyze_request may run twice so the plan can
# schedule e.g. "extract requirements" and "analyse feasibility" as separate model-backed steps.
MAX_USES: dict[str, int] = {"analyze_request": 2}


class AgentRequest(BaseModel):
    request: Annotated[str, StringConstraints(strip_whitespace=True, min_length=10, max_length=5000)]


class Assumption(BaseModel):
    statement: str = Field(min_length=1)
    category: Literal[
        "assumption", "illustrative_data", "requires_confirmation", "constraint_conflict"
    ]
    impact: str = ""


class PlannedTask(BaseModel):
    """Task as proposed by the LLM. Runtime fields are deliberately absent so the model
    cannot pre-mark a task as completed (unknown extra keys are ignored)."""

    id: int = Field(ge=1)
    title: str = Field(min_length=1, max_length=150)
    description: str = Field(min_length=1)
    handler: HandlerName
    dependencies: list[int] = Field(default_factory=list)
    expected_output: str = Field(min_length=1)


class Task(PlannedTask):
    """Task with runtime state, owned by Python."""

    status: TaskStatus = "pending"
    result_summary: str | None = None
    error: str | None = None

    @classmethod
    def from_planned(cls, planned: PlannedTask) -> "Task":
        return cls(**planned.model_dump())


class Plan(BaseModel):
    document_type: str = Field(min_length=1)
    title: str = Field(min_length=1)
    audience: str = Field(min_length=1)
    known_facts: list[str] = Field(default_factory=list)
    requirements: list[str] = Field(default_factory=list)
    constraints: list[str] = Field(default_factory=list)
    assumptions: list[Assumption] = Field(default_factory=list)
    open_questions: list[str] = Field(default_factory=list)
    required_sections: list[str] = Field(min_length=1, max_length=20)
    tasks: list[PlannedTask] = Field(min_length=MIN_TASKS, max_length=MAX_TASKS)

    @field_validator("required_sections")
    @classmethod
    def clean_sections(cls, sections: list[str]) -> list[str]:
        seen: set[str] = set()
        cleaned: list[str] = []
        for section in sections:
            name = section.strip()
            if name and name.lower() not in seen:
                seen.add(name.lower())
                cleaned.append(name)
        if not cleaned:
            raise ValueError("required_sections must contain at least one non-empty name")
        return cleaned

    @model_validator(mode="after")
    def validate_tasks(self) -> "Plan":
        # Execution is sequential, so a dependency must point at an EARLIER task.
        # This also makes cycles impossible.
        seen: list[int] = []
        for task in self.tasks:
            if task.id in seen:
                raise ValueError(f"Duplicate task id {task.id}")
            for dep in task.dependencies:
                if dep == task.id:
                    raise ValueError(f"Task {task.id} cannot depend on itself")
                if dep not in seen:
                    raise ValueError(
                        f"Task {task.id} depends on {dep}, which is unknown or not earlier "
                        "in the task list (tasks run sequentially)"
                    )
            seen.append(task.id)

        handlers = [task.handler for task in self.tasks]
        for name in sorted(set(handlers)):
            limit = MAX_USES.get(name, 1)
            if handlers.count(name) > limit:
                raise ValueError(f"Handler '{name}' may be used at most {limit} time(s) per plan")
        missing = [h for h in REQUIRED_PIPELINE if h not in handlers]
        if missing:
            raise ValueError(f"Plan must include handlers: {missing}")
        positions = [handlers.index(h) for h in REQUIRED_PIPELINE]
        if positions != sorted(positions):
            raise ValueError("Tasks must be ordered: draft_document, review_document, generate_docx")
        if handlers[-1] != "generate_docx":
            raise ValueError("generate_docx must be the last task")
        # Analysis must inform the draft and the review, so it cannot run after drafting.
        drafting = handlers.index("draft_document")
        if any(h in ("analyze_request", "resolve_assumptions") for h in handlers[drafting:]):
            raise ValueError("analyze_request and resolve_assumptions tasks must come before draft_document")
        # resolve_assumptions folds analysis results into the register; a later analysis would be lost.
        if "resolve_assumptions" in handlers:
            resolving = handlers.index("resolve_assumptions")
            if "analyze_request" in handlers[resolving:]:
                raise ValueError("resolve_assumptions must come after every analyze_request task")
        return self


class ReviewReport(BaseModel):
    score: int = Field(ge=0, le=100)
    identified_gaps: list[str] = Field(default_factory=list)
    failed_checks: list[str] = Field(default_factory=list)
    sections_to_revise: list[str] = Field(default_factory=list)
    recommended_corrections: list[str] = Field(default_factory=list)
    remaining_issues: list[str] = Field(default_factory=list)
    requirements_covered: list[str] = Field(default_factory=list)


class DocumentTable(BaseModel):
    columns: list[str] = Field(min_length=1)
    rows: list[list[str]] = Field(default_factory=list)

    @field_validator("rows", mode="before")
    @classmethod
    def stringify_cells(cls, rows):
        # LLMs often emit numbers in table cells; accept them as text.
        if isinstance(rows, list):
            return [[str(c) for c in row] if isinstance(row, list) else row for row in rows]
        return rows

    @model_validator(mode="after")
    def align_rows(self) -> "DocumentTable":
        width = len(self.columns)
        self.rows = [(row + [""] * width)[:width] for row in self.rows]
        return self


class DocumentSection(BaseModel):
    heading: str = Field(min_length=1)
    content: str = ""
    bullets: list[str] = Field(default_factory=list)
    table: DocumentTable | None = None


class AnalysisNote(BaseModel):
    """Result of one model-backed analysis task (analyze_request handler)."""

    summary: str = Field(min_length=1)
    findings: list[str] = Field(default_factory=list)
    conflicts: list[str] = Field(default_factory=list)
    open_questions: list[str] = Field(default_factory=list)


class DocumentContent(BaseModel):
    title: str = Field(min_length=1)
    sections: list[DocumentSection] = Field(min_length=1)


class RevisionResult(BaseModel):
    """Only the revised or newly added sections, matched to the draft by heading."""

    summary: str = ""
    sections: list[DocumentSection] = Field(min_length=1)


class AgentResponse(BaseModel):
    run_id: str
    status: Literal["completed", "completed_with_warnings", "failed"]
    summary: str
    document_type: str | None = None
    plan: list[Task] = Field(default_factory=list)
    assumptions: list[Assumption] = Field(default_factory=list)
    open_questions: list[str] = Field(default_factory=list)
    review: ReviewReport | None = None
    revisions_performed: int = Field(default=0, ge=0, le=1)
    fallback_used: bool = False
    warnings: list[str] = Field(default_factory=list)
    provider: str
    model: str
    document_filename: str | None = None
    download_url: str | None = None
    error: str | None = None
