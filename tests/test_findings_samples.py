"""Runs every mock sample under samples/ through the REAL pipeline (mock evidence
injected via adapters._KNOWN, exactly what run_findings.py does) and checks the
outcome against resale_listing_ai.findings.EXPECTED_STATUS_BY_SAMPLE. This is what makes
that mapping a real regression guard rather than documentation that can silently
drift from pipeline.gate()'s actual behavior.

Skips (not fails) if samples/ hasn't been generated yet (run
samples/_build_samples.py) — matches the existing "skip when the fixture data
isn't there" convention for the git-ignored real product folders.
"""

import pytest

from resale_listing_ai import adapters
from resale_listing_ai.cost import CostLedger
from resale_listing_ai.db import Store
from resale_listing_ai.findings import (
    EXPECTED_STATUS_BY_SAMPLE, discover_mock_submissions, load_mock_submission,
    mock_evidence_to_known, process_sample,
)


@pytest.fixture(autouse=True)
def _stub_mode(monkeypatch):
    monkeypatch.setenv("RESALE_LISTING_AI_STUBS", "1")
    monkeypatch.setenv("RESALE_LISTING_AI_DB_MODE", "sqlite")
    monkeypatch.delenv("DATABASE_URL", raising=False)


@pytest.fixture
def store():
    s = Store.connect()
    s.ensure_schema()
    yield s
    s.close()


def _mock_sample_folders():
    return discover_mock_submissions()


@pytest.mark.parametrize("folder", _mock_sample_folders(), ids=lambda d: d.name)
def test_mock_sample_matches_expected_gate_outcome(folder, store, monkeypatch):
    submission, hint, evidence = load_mock_submission(folder)
    if hint not in EXPECTED_STATUS_BY_SAMPLE:
        pytest.skip(f"{hint} has no expected-status entry in EXPECTED_STATUS_BY_SAMPLE")

    monkeypatch.setitem(adapters._KNOWN, hint, mock_evidence_to_known(evidence))

    outcome = process_sample(submission, store, CostLedger(), set())

    assert outcome["result"]["status"] == EXPECTED_STATUS_BY_SAMPLE[hint]


def test_every_expected_status_entry_has_a_matching_sample_folder():
    """Guards against EXPECTED_STATUS_BY_SAMPLE silently going stale (a renamed/
    deleted sample folder leaving an orphaned entry no test ever exercises)."""
    folders = _mock_sample_folders()
    if not folders:
        pytest.skip("samples/ not generated yet — run samples/_build_samples.py")
    names = {d.name for d in folders}
    assert set(EXPECTED_STATUS_BY_SAMPLE) <= names
