import pytest
from pydantic import ValidationError

from app.schemas import DocumentSection, DocumentTable, Plan


def task(id_, handler, deps=()):
    return {
        "id": id_, "title": f"Task {id_}", "description": "Do the thing",
        "handler": handler, "dependencies": list(deps), "expected_output": "Something",
    }


def plan_dict(tasks=None):
    return {
        "document_type": "report", "title": "T", "audience": "A",
        "required_sections": ["Summary", "summary", " Risks "],
        "tasks": tasks or [
            task(1, "analyze_request"),
            task(2, "draft_document", [1]),
            task(3, "review_document", [2]),
            task(4, "generate_docx", [3]),
        ],
    }


def test_valid_plan_and_section_cleanup():
    plan = Plan.model_validate(plan_dict())
    assert len(plan.tasks) == 4
    assert plan.required_sections == ["Summary", "Risks"]  # trimmed, de-duplicated
    assert plan.open_questions == [] and plan.assumptions == []


def test_runtime_fields_from_llm_are_ignored():
    data = plan_dict()
    data["tasks"][0]["status"] = "completed"
    plan = Plan.model_validate(data)
    assert not hasattr(plan.tasks[0], "status")


@pytest.mark.parametrize("tasks, fragment", [
    ([task(1, "draft_document"), task(1, "review_document"), task(3, "generate_docx")], "Duplicate"),
    ([task(1, "draft_document", [9]), task(2, "review_document"), task(3, "generate_docx")], "unknown or not earlier"),
    ([task(1, "draft_document", [2]), task(2, "review_document"), task(3, "generate_docx")], "unknown or not earlier"),
    ([task(1, "draft_document", [1]), task(2, "review_document"), task(3, "generate_docx")], "itself"),
    ([task(1, "draft_document"), task(2, "review_document")], "at least"),  # below MIN_TASKS
    ([task(1, "draft_document"), task(2, "review_document"), task(3, "review_document"), task(4, "generate_docx")], "at most"),
    ([task(1, "draft_document"), task(2, "generate_docx"), task(3, "review_document")], "ordered"),
    ([task(1, "analyze_request"), task(2, "draft_document"), task(3, "review_document")], "must include"),
    ([task(1, "analyze_request"), task(2, "analyze_request"), task(3, "analyze_request"), task(4, "draft_document"), task(5, "review_document"), task(6, "generate_docx")], "at most 2"),
    ([task(1, "draft_document"), task(2, "review_document"), task(3, "generate_docx"), task(4, "resolve_assumptions")], "must be the last"),
    ([task(1, "draft_document"), task(2, "review_document"), task(3, "run_shell"), task(4, "generate_docx")], "handler"),
])
def test_invalid_plans_are_rejected(tasks, fragment):
    with pytest.raises(ValidationError) as exc:
        Plan.model_validate(plan_dict(tasks))
    assert fragment in str(exc.value)


def test_too_many_tasks_rejected():
    handlers = ["analyze_request", "resolve_assumptions", "draft_document", "review_document", "generate_docx"]
    tasks = [task(i + 1, h) for i, h in enumerate(handlers)] + [task(6, "analyze_request"), task(7, "analyze_request"), task(8, "analyze_request")]
    with pytest.raises(ValidationError):
        Plan.model_validate(plan_dict(tasks))


def test_table_cells_are_coerced_and_aligned():
    table = DocumentTable.model_validate({"columns": ["Item", "Cost"], "rows": [["Licence", 5000], ["Only one"]]})
    assert table.rows == [["Licence", "5000"], ["Only one", ""]]
    assert DocumentSection(heading="H").table is None


def test_two_analysis_steps_are_allowed():
    tasks = [task(1, "analyze_request"), task(2, "analyze_request", [1]), task(3, "draft_document", [2]),
             task(4, "review_document", [3]), task(5, "generate_docx", [4])]
    assert len(Plan.model_validate(plan_dict(tasks)).tasks) == 5


def test_all_runtime_keys_in_model_output_are_ignored():
    data = plan_dict()
    for t in data["tasks"]:
        t.update(status="completed", error="forged", result_summary="forged")
    plan = Plan.model_validate(data)
    assert all(not hasattr(t, name) for t in plan.tasks for name in ("status", "error", "result_summary"))


# ---- External review, finding 5: analysis and assumption steps must precede drafting ----

@pytest.mark.parametrize("tasks, fragment", [
    # analysis after drafting
    ([task(1, "draft_document"), task(2, "analyze_request", [1]), task(3, "review_document", [2]), task(4, "generate_docx", [3])],
     "before draft_document"),
    # analysis after review
    ([task(1, "draft_document"), task(2, "review_document", [1]), task(3, "analyze_request", [2]), task(4, "generate_docx", [3])],
     "before draft_document"),
    # resolve_assumptions after drafting
    ([task(1, "draft_document"), task(2, "resolve_assumptions", [1]), task(3, "review_document", [2]), task(4, "generate_docx", [3])],
     "before draft_document"),
    # resolve_assumptions runs before a later analysis, so that analysis would never reach the register
    ([task(1, "analyze_request"), task(2, "resolve_assumptions", [1]), task(3, "analyze_request", [2]),
      task(4, "draft_document", [3]), task(5, "review_document", [4]), task(6, "generate_docx", [5])],
     "after every analyze_request"),
])
def test_analysis_steps_must_come_before_drafting_and_resolution_after_analysis(tasks, fragment):
    with pytest.raises(ValidationError) as exc:
        Plan.model_validate(plan_dict(tasks))
    assert fragment in str(exc.value)


def test_valid_orderings_of_the_early_steps_are_still_accepted():
    for early in (["analyze_request"], ["analyze_request", "analyze_request"],
                  ["analyze_request", "resolve_assumptions"], ["analyze_request", "analyze_request", "resolve_assumptions"],
                  ["resolve_assumptions"], []):
        handlers = early + ["draft_document", "review_document", "generate_docx"]
        tasks = [task(i + 1, h, [i] if i else []) for i, h in enumerate(handlers)]
        assert len(Plan.model_validate(plan_dict(tasks)).tasks) == len(handlers)
