"""Check the evaluator's judgments independently of retrieval implementation."""

from retrieval_quality import assess, summarize


CASE = {"id": "example", "split": "development", "kind": "relevant", "required": ["value_communication"], "allowed": ["segmentation"], "evidence_pages": [143, 144]}


def response(ids, pages):
    return {"frameworks": [{"id": identity} for identity in ids], "results": [{"page": page} for page in pages]}


def test_membership_matters_even_when_counts_match():
    row = assess(CASE, response(["behavioral_pricing"], [143]))
    assert row["missing"] == ["value_communication"]
    assert row["unwanted"] == ["behavioral_pricing"]
    assert not row["passed"]


def test_allowed_support_and_exact_source_pages_pass():
    row = assess(CASE, response(["value_communication", "segmentation"], [143]))
    assert row["passed"]
    wrong_source = assess(CASE, response(["value_communication"], [156]))
    assert not wrong_source["evidence_hit"] and not wrong_source["passed"]


def test_irrelevant_cases_require_both_outputs_to_be_empty():
    case = {**CASE, "kind": "irrelevant", "required": [], "allowed": [], "evidence_pages": []}
    assert assess(case, response([], []))["passed"]
    assert not assess(case, response(["value_communication"], []))["passed"]
    assert not assess(case, response([], [143]))["passed"]


def test_summary_does_not_count_optional_frameworks_as_required_hits():
    row = assess(CASE, response(["segmentation", "behavioral_pricing"], [143]))
    summary = summarize([row])
    assert summary["required_recall"] == 0
    assert summary["allowed_precision"] == 0.5
    assert summary["unwanted_inclusions"] == 1
    assert summary["passed"] == 0
