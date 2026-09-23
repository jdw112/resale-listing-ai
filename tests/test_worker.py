"""Tests for resale_listing_ai/worker.py — the queue/worker runtime wrapping
pipeline._stage_ingest/_stage_images (System Design §8/§9). Offline throughout:
RESALE_LISTING_AI_STUBS=1 adapters, sqlite Store, in-memory LocalQueue.
"""

import pytest

from resale_listing_ai import worker
from resale_listing_ai.cost import CostLedger
from resale_listing_ai.db import Store
from resale_listing_ai.queue import LocalQueue

REC = {"submission_id": "w1", "hint": "nest-doorbell", "images": ["a"], "notes": None,
       "provided_condition_grade": "Open Box"}
UNK = {"submission_id": "w2", "hint": "mystery", "images": ["a"], "notes": None,
       "provided_condition_grade": "Used - Fair"}


@pytest.fixture(autouse=True)
def _stub_adapters(monkeypatch):
    monkeypatch.setenv("RESALE_LISTING_AI_STUBS", "1")
    # setenv, not delenv: _load_dotenv() uses os.environ.setdefault(), which
    # would silently re-populate a deleted var from a real repo .env (e.g.
    # RESALE_LISTING_AI_DB_MODE=postgres uncommented for live-demo use).
    monkeypatch.setenv("RESALE_LISTING_AI_DB_MODE", "sqlite")
    monkeypatch.delenv("DATABASE_URL", raising=False)


@pytest.fixture
def store():
    s = Store.connect()
    s.ensure_schema()
    yield s
    s.close()


@pytest.fixture
def queues():
    ingest_dlq = LocalQueue("ingest-dlq")
    ingest = LocalQueue("ingest-queue", dlq=ingest_dlq, max_receive_count=2)
    image_dlq = LocalQueue("image-dlq")
    image = LocalQueue("image-queue", dlq=image_dlq, max_receive_count=2)
    return {"ingest": ingest, "ingest_dlq": ingest_dlq, "image": image, "image_dlq": image_dlq}


def _no_sleep(*a, **k):
    pass


# ---------------------------------------------------------------------------
# End-to-end: ingest-queue -> ingest worker -> image-queue -> image worker
# ---------------------------------------------------------------------------

def test_run_once_processes_an_accepted_submission_end_to_end(store, queues):
    queues["ingest"].send({"submission": REC})
    ledger = CostLedger()

    results = worker.run_once(store, queues, ledger=ledger, sleep=_no_sleep)

    assert any(r.get("status") == "Accepted" for r in results)
    final = store.get_submission_result("w1")
    assert final["status"] == "Accepted"
    assert store.count("product") == 1
    assert store.count("listing") == 1
    assert len(queues["image"]) == 0  # image job consumed, not left behind


def test_run_once_finalizes_a_rejected_submission_without_touching_image_queue(store, queues):
    queues["ingest"].send({"submission": UNK})
    ledger = CostLedger()

    worker.run_once(store, queues, ledger=ledger, sleep=_no_sleep)

    final = store.get_submission_result("w2")
    assert final["status"] in ("Clarification required", "Rejected")
    assert store.count("product") == 0
    assert len(queues["image"]) == 0


def test_a_received_only_marker_does_not_block_first_time_processing(store, queues):
    """The intake API (resale_listing_ai/api.py) writes a state="RECEIVED" row at POST
    time, before any worker runs. That marker must NOT be mistaken for "already
    processed" — only INGESTED/DONE mean real work happened."""
    store.mark_submission_received("w1")
    queues["ingest"].send({"submission": REC})
    ledger = CostLedger()

    results = worker.run_once(store, queues, ledger=ledger, sleep=_no_sleep)

    assert any(r.get("status") == "Accepted" for r in results)
    assert store.get_submission_result("w1") is not None
    assert store.count("product") == 1


def test_ingest_worker_marks_ingested_and_enqueues_an_image_job(store, queues):
    queues["ingest"].send({"submission": REC})
    ledger = CostLedger()

    worker.run_ingest_worker_once(store, queues["ingest"], queues["image"], ledger=ledger, sleep=_no_sleep)

    state = store.get_submission_state("w1")
    assert state["state"] == "INGESTED"
    assert len(queues["image"]) == 1
    assert store.get_submission_result("w1") is None  # not DONE yet -> images still pending


# ---------------------------------------------------------------------------
# Idempotency: a redelivered message (at-least-once) is a no-op
# ---------------------------------------------------------------------------

def test_redelivered_ingest_message_is_a_safe_no_op(store, queues):
    """Simulates at-least-once redelivery: the same submission is enqueued and
    fully processed once, then the SAME message body shows up again (SQS
    redelivery after a visibility-timeout lapse, or the API's own retry) — the
    second processing must not mint a new SKU/product row or duplicate cost rows."""
    queues["ingest"].send({"submission": REC})
    ledger = CostLedger()
    worker.run_once(store, queues, ledger=ledger, sleep=_no_sleep)

    first_result = store.get_submission_result("w1")
    rows_after_first = len(store.cost_rows("w1"))
    assert rows_after_first > 0

    # redelivery: the same submission arrives on ingest-queue again
    queues["ingest"].send({"submission": REC})
    worker.run_once(store, queues, ledger=CostLedger(), sleep=_no_sleep)

    assert store.get_submission_result("w1") == first_result
    assert len(store.cost_rows("w1")) == rows_after_first  # no new rows -> no re-run
    assert store.count("product") == 1
    assert store.count("listing") == 1
    assert store.count("submission") == 1


def test_redelivered_ingest_message_between_ingest_and_images_does_not_remint_sku(store, queues):
    """A message redelivered AFTER ingest completed but BEFORE images ran (the
    trickiest window: state=INGESTED, not yet DONE) must not create a second
    listing/SKU for the same submission."""
    queues["ingest"].send({"submission": REC})
    ledger = CostLedger()
    worker.run_ingest_worker_once(store, queues["ingest"], queues["image"], ledger=ledger, sleep=_no_sleep)
    sku_after_first_ingest = store.get_submission_state("w1")["sku"]

    # redelivered ingest message while still only INGESTED (images not run yet)
    queues["ingest"].send({"submission": REC})
    worker.run_ingest_worker_once(store, queues["ingest"], queues["image"], ledger=ledger, sleep=_no_sleep)

    assert store.get_submission_state("w1")["sku"] == sku_after_first_ingest
    assert store.count("listing") == 1   # _stage_ingest already persists the listing row
                                          # (image fields land later) -> still just one, not two
    assert len(queues["image"]) == 1     # only ONE image job was ever enqueued


def test_redelivered_image_message_after_completion_is_a_no_op(store, queues):
    queues["ingest"].send({"submission": REC})
    ledger = CostLedger()
    worker.run_once(store, queues, ledger=ledger, sleep=_no_sleep)
    first_result = store.get_submission_result("w1")

    # redeliver the image job as if it were never deleted (worker crash after
    # finishing but before ack)
    state = store.get_submission_state("w1")
    queues["image"].send({"submission": REC, "product_key": state["product_key"],
                           "sku": state["sku"], "reused": False})
    worker.run_image_worker_once(store, queues["image"], ledger=CostLedger(), sleep=_no_sleep)

    assert store.get_submission_result("w1") == first_result
    assert store.count("listing") == 1


# ---------------------------------------------------------------------------
# Retry: exponential backoff + jitter, logged to the cost ledger
# ---------------------------------------------------------------------------

def test_transient_failure_retries_then_succeeds_and_logs_each_attempt(store, queues, monkeypatch):
    from resale_listing_ai import pipeline

    calls = {"n": 0}
    real_stage_ingest = pipeline._stage_ingest

    def flaky_stage_ingest(submission, store_, ledger_, skus_):
        calls["n"] += 1
        if calls["n"] < 3:
            raise ConnectionError("transient")
        return real_stage_ingest(submission, store_, ledger_, skus_)

    monkeypatch.setattr(pipeline, "_stage_ingest", flaky_stage_ingest)
    queues["ingest"].send({"submission": REC})
    ledger = CostLedger()
    sleeps = []

    worker.run_ingest_worker_once(
        store, queues["ingest"], queues["image"], ledger=ledger, max_attempts=5,
        sleep=lambda s: sleeps.append(s), rand=lambda a, b: 0.0)

    assert calls["n"] == 3
    worker_rows = [r for r in ledger.rows if r["service"] == "worker"]
    assert len(worker_rows) == 3
    assert [r["retry"] for r in worker_rows] == [False, True, True]
    assert len(sleeps) == 2
    assert sleeps[1] > sleeps[0]  # exponential backoff, strictly increasing
    assert store.get_submission_state("w1")["state"] == "INGESTED"


def test_exhausting_retries_leaves_message_in_queue_for_redelivery(store, queues, monkeypatch):
    from resale_listing_ai import pipeline

    def always_fails(submission, store_, ledger_, skus_):
        raise ConnectionError("still down")

    monkeypatch.setattr(pipeline, "_stage_ingest", always_fails)
    queues["ingest"].send({"submission": REC})
    ledger = CostLedger()

    worker.run_ingest_worker_once(
        store, queues["ingest"], queues["image"], ledger=ledger, max_attempts=2, sleep=_no_sleep)

    assert store.get_submission_state("w1") is None  # never completed
    assert len(queues["ingest"]) == 1                # message NOT deleted -> stays for redelivery/DLQ


# ---------------------------------------------------------------------------
# Completion callback: optional POST to callback_url when a submission
# reaches DONE (Task 4)
# ---------------------------------------------------------------------------

from types import SimpleNamespace

REC_WITH_CALLBACK = {**REC, "submission_id": "w4", "callback_url": "https://example.test/hook"}
UNK_WITH_CALLBACK = {**UNK, "submission_id": "w5", "callback_url": "https://example.test/hook"}


def _fake_ok_response():
    return SimpleNamespace(raise_for_status=lambda: None)


def test_callback_posted_on_accepted_completion(store, queues, monkeypatch):
    calls = []

    def fake_post(url, json=None, timeout=None):
        calls.append((url, json))
        return _fake_ok_response()

    monkeypatch.setattr(worker.requests, "post", fake_post)
    queues["ingest"].send({"submission": REC_WITH_CALLBACK})

    worker.run_once(store, queues, ledger=CostLedger(), sleep=_no_sleep)

    assert len(calls) == 1
    url, body = calls[0]
    assert url == "https://example.test/hook"
    assert body["state"] == "PUBLISHED"
    assert body["product"]["Brand"]["value"] == "Acme"


def test_callback_posted_on_rejected_completion(store, queues, monkeypatch):
    calls = []

    def fake_post(url, json=None, timeout=None):
        calls.append((url, json))
        return _fake_ok_response()

    monkeypatch.setattr(worker.requests, "post", fake_post)
    queues["ingest"].send({"submission": UNK_WITH_CALLBACK})

    worker.run_once(store, queues, ledger=CostLedger(), sleep=_no_sleep)

    assert len(calls) == 1
    url, body = calls[0]
    assert body["state"] in ("REJECTED", "CLARIFICATION_REQUIRED")
    assert body["product"] is None


def test_callback_delivery_retries_then_gives_up_without_failing_submission(store, queues, monkeypatch, capsys):
    def always_fails(url, json=None, timeout=None):
        raise ConnectionError("down")

    monkeypatch.setattr(worker.requests, "post", always_fails)
    queues["ingest"].send({"submission": REC_WITH_CALLBACK})

    results = worker.run_once(store, queues, ledger=CostLedger(), sleep=_no_sleep, max_attempts=2)

    assert any(r.get("status") == "Accepted" for r in results)
    assert store.get_submission_result("w4")["status"] == "Accepted"
    assert "callback" in capsys.readouterr().err


def test_callback_not_refired_on_redelivered_completed_submission(store, queues, monkeypatch):
    calls = []

    def fake_post(url, json=None, timeout=None):
        calls.append((url, json))
        return _fake_ok_response()

    monkeypatch.setattr(worker.requests, "post", fake_post)
    queues["ingest"].send({"submission": REC_WITH_CALLBACK})
    worker.run_once(store, queues, ledger=CostLedger(), sleep=_no_sleep)
    assert len(calls) == 1

    state = store.get_submission_state("w4")
    queues["image"].send({"submission": REC_WITH_CALLBACK, "product_key": state["product_key"],
                           "sku": state["sku"], "reused": False})
    worker.run_image_worker_once(store, queues["image"], ledger=CostLedger(), sleep=_no_sleep)

    assert len(calls) == 1  # no second delivery


def test_run_once_persists_owner_and_parent_through_both_stages(store, queues):
    owned = {**REC, "submission_id": "w6", "user_id": 42, "parent_submission_id": None}
    queues["ingest"].send({"submission": owned})

    worker.run_once(store, queues, ledger=CostLedger(), sleep=_no_sleep)

    state = store.get_submission_state("w6")
    assert state["user_id"] == 42
    assert state["parent_submission_id"] is None


def test_run_once_persists_owner_and_parent_for_a_terminal_rejected_submission(store, queues):
    owned = {**UNK, "submission_id": "w7", "user_id": 42, "parent_submission_id": "w6"}
    queues["ingest"].send({"submission": owned})

    worker.run_once(store, queues, ledger=CostLedger(), sleep=_no_sleep)

    state = store.get_submission_state("w7")
    assert state["user_id"] == 42
    assert state["parent_submission_id"] == "w6"
