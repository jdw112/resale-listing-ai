"""Tests for resale_listing_ai/findings.py's compute_findings() — PRD §4.2 measures,
reported separately (never one rolled-up success rate). Pure function over a list
of already-computed per-submission records; no I/O.
"""


def _record(submission_id, status, cost_cad, *, ingest_s=0.01, images_s=0.0,
            cost_rows=None, expected_status=None, provided="Open Box", ai_suggested="Open Box"):
    listing = None
    if status.startswith("Accepted"):
        listing = {
            "ProvidedConditionGrade": {"value": provided, "confidence": 1.0, "source": "provided"},
            "AISuggestedConditionGrade": {"value": ai_suggested, "confidence": 0.8, "source": "vision"},
        }
    return {
        "submission_id": submission_id,
        "expected_status": expected_status,
        "result": {"submission_id": submission_id, "status": status, "cost_cad": cost_cad,
                   "listing": listing},
        "cost_rows": cost_rows or [],
        "ingest_seconds": ingest_s, "images_seconds": images_s, "total_seconds": ingest_s + images_s,
    }


def test_compute_findings_totals_tokens_by_stage_and_overall():
    from resale_listing_ai.findings import compute_findings

    rows_a = [
        {"stage": "identify", "service": "openai_vision", "cost_cad": 0.01,
         "input_tokens": 1000, "output_tokens": 200, "model": "gpt-4o-mini"},
        {"stage": "copy", "service": "openai_copy", "cost_cad": 0.01,
         "input_tokens": 300, "output_tokens": 400, "model": "gpt-4o-mini"},
        {"stage": "identify", "service": "local_ocr", "cost_cad": 0.0},  # no tokens
    ]
    rows_b = [
        {"stage": "identify", "service": "openai_vision", "cost_cad": 0.01,
         "input_tokens": 500, "output_tokens": 100, "model": "gpt-4o-mini"},
    ]
    records = [
        _record("s1", "Accepted with unknowns", 0.02, cost_rows=rows_a),
        _record("s2", "Accepted with unknowns", 0.01, cost_rows=rows_b),
    ]

    findings = compute_findings(records)

    assert findings["tokens_by_stage"] == {
        "identify": {"input_tokens": 1500, "output_tokens": 300},
        "copy": {"input_tokens": 300, "output_tokens": 400},
    }
    assert findings["total_input_tokens"] == 1800
    assert findings["total_output_tokens"] == 700


def test_compute_findings_separates_completed_from_exceptions():
    from resale_listing_ai.findings import compute_findings

    records = [
        _record("s1", "Accepted with unknowns", 0.14),
        _record("s2", "Rejected", 0.03),
        _record("s3", "Clarification required", 0.03),
    ]

    findings = compute_findings(records)

    assert findings["total_submissions"] == 3
    assert findings["completed_count"] == 1
    assert findings["exceptions_count"] == 2


def test_compute_findings_exceptions_counted_by_status_not_rolled_into_one_rate():
    from resale_listing_ai.findings import compute_findings

    records = [
        _record("s1", "Rejected", 0.03),
        _record("s2", "Rejected", 0.03),
        _record("s3", "Clarification required", 0.03),
    ]

    findings = compute_findings(records)

    assert findings["exceptions_by_status"] == {"Rejected": 2, "Clarification required": 1}


def test_compute_findings_avg_cost_per_completed_listing_excludes_exceptions():
    from resale_listing_ai.findings import compute_findings

    records = [
        _record("s1", "Accepted with unknowns", 0.14),
        _record("s2", "Accepted with unknowns", 0.08),
        _record("s3", "Rejected", 0.03),  # must NOT drag the average down
    ]

    findings = compute_findings(records)

    assert findings["avg_cost_per_completed_listing_cad"] == 0.11


def test_compute_findings_meets_cost_target_true_when_at_or_under_target():
    from resale_listing_ai.findings import compute_findings

    findings = compute_findings([_record("s1", "Accepted with unknowns", 0.20)])

    assert findings["cost_target_cad"] == 0.25
    assert findings["meets_cost_target"] is True


def test_compute_findings_meets_cost_target_false_when_over_target():
    from resale_listing_ai.findings import compute_findings

    findings = compute_findings([_record("s1", "Accepted with unknowns", 0.40)])

    assert findings["meets_cost_target"] is False


def test_compute_findings_no_completed_listings_cost_target_is_undetermined_not_a_crash():
    from resale_listing_ai.findings import compute_findings

    findings = compute_findings([_record("s1", "Rejected", 0.03)])

    assert findings["avg_cost_per_completed_listing_cad"] is None
    assert findings["meets_cost_target"] is None


def test_compute_findings_cost_by_stage_sums_across_records():
    from resale_listing_ai.findings import compute_findings

    records = [
        _record("s1", "Accepted with unknowns", 0.14, cost_rows=[
            {"stage": "identify", "cost_cad": 0.03}, {"stage": "images", "cost_cad": 0.05}]),
        _record("s2", "Accepted with unknowns", 0.08, cost_rows=[
            {"stage": "identify", "cost_cad": 0.00}, {"stage": "images", "cost_cad": 0.05}]),
    ]

    findings = compute_findings(records)

    assert findings["cost_by_stage_cad"] == {"identify": 0.03, "images": 0.10}


def test_compute_findings_avg_seconds_reported_per_stage():
    from resale_listing_ai.findings import compute_findings

    records = [
        _record("s1", "Accepted with unknowns", 0.14, ingest_s=0.02, images_s=0.10),
        _record("s2", "Accepted with unknowns", 0.08, ingest_s=0.04, images_s=0.20),
    ]

    findings = compute_findings(records)

    assert findings["avg_ingest_seconds"] == 0.03
    assert findings["avg_images_seconds"] == 0.15
    assert findings["avg_total_seconds"] == 0.18


def test_compute_findings_manual_intervention_counts_clarification_required():
    from resale_listing_ai.findings import compute_findings

    records = [_record("s1", "Clarification required", 0.03), _record("s2", "Accepted with unknowns", 0.10)]

    findings = compute_findings(records)

    assert findings["manual_intervention_count"] == 1
    assert findings["manual_intervention_rate"] == 0.5


def test_compute_findings_manual_intervention_counts_condition_grade_mismatch():
    from resale_listing_ai.findings import compute_findings

    records = [_record("s1", "Accepted with unknowns", 0.10, provided="Open Box", ai_suggested="Used - Good")]

    findings = compute_findings(records)

    assert findings["manual_intervention_count"] == 1


def test_compute_findings_manual_intervention_excludes_matching_condition_grades():
    from resale_listing_ai.findings import compute_findings

    records = [_record("s1", "Accepted with unknowns", 0.10, provided="Open Box", ai_suggested="Open Box")]

    findings = compute_findings(records)

    assert findings["manual_intervention_count"] == 0


def test_compute_findings_pipeline_correctness_accuracy_against_expected_status():
    from resale_listing_ai.findings import compute_findings

    records = [
        _record("s1", "Rejected", 0.03, expected_status="Rejected"),               # matches
        _record("s2", "Accepted with unknowns", 0.10, expected_status="Rejected"),  # does NOT match
        _record("s3", "Accepted with unknowns", 0.10, expected_status=None),        # no expectation -> excluded
    ]

    findings = compute_findings(records)

    assert findings["pipeline_correctness"]["tested"] == 2
    assert findings["pipeline_correctness"]["matched_expected"] == 1
    assert findings["pipeline_correctness"]["accuracy"] == 0.5


def test_compute_findings_empty_records_returns_sane_zeros_not_a_crash():
    from resale_listing_ai.findings import compute_findings

    findings = compute_findings([])

    assert findings["total_submissions"] == 0
    assert findings["completed_count"] == 0
    assert findings["exceptions_count"] == 0
    assert findings["avg_cost_per_completed_listing_cad"] is None
    assert findings["meets_cost_target"] is None
    assert findings["pipeline_correctness"]["tested"] == 0
