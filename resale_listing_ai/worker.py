"""Worker runtime — wraps pipeline._stage_ingest/_stage_images in queue consumers
(System Design §3 WORKER PLANE, §8 Queue & Worker Design, §9 Error Handling &
Retry Logic). Two independent loops: the ingest worker drains ingest-queue and,
on success, enqueues an image-queue job; the image worker drains image-queue and
finalizes the submission. Both are:

  - At-least-once safe: idempotency is checked via store.get_submission_state
    BEFORE any stage work happens (INGESTED / DONE checkpoints — see db.py), so a
    redelivered message (worker crash, SQS visibility-timeout redelivery, a
    manual replay) is a no-op rather than a duplicate row or double charge.
  - Retried on transient failure with exponential backoff + jitter (bounded),
    each attempt logged to the cost ledger under service="worker" (System Design
    §9: "Retry with exponential backoff + jitter; each retry logged to the cost
    ledger"). Exhausting retries leaves the message in the queue for its own
    visibility-timeout-driven redelivery — and eventually the DLQ via
    max_receive_count — rather than silently swallowing the failure.

Run `python -m resale_listing_ai.worker --run-once` for a local demo: drains whatever is
currently queued once, then exits — no long-running daemon needed.
"""

import random
import sys
import time

import requests

from . import pipeline
from .submission_view import submission_status_body

# Processing a message is not itself a billed API call (the adapters underneath
# it already log their own real costs) — logged at $0 for observability/audit,
# same convention as barcode_read's deterministic, free stage.
WORKER_UNIT_COST_CAD = 0.0


def _retry(fn, *, ledger, submission_id, stage, max_attempts=3, base_delay=0.05, jitter=0.05,
           sleep=time.sleep, rand=random.uniform, retry_exceptions=Exception):
    last_exc = None
    for attempt in range(max_attempts):
        try:
            result = fn()
        except retry_exceptions as exc:
            last_exc = exc
            if ledger is not None:
                ledger.add(submission_id, stage, "worker", WORKER_UNIT_COST_CAD, retry=(attempt > 0))
            if attempt < max_attempts - 1:
                sleep(base_delay * (2 ** attempt) + rand(0, jitter))
            continue
        else:
            if ledger is not None:
                ledger.add(submission_id, stage, "worker", WORKER_UNIT_COST_CAD, retry=(attempt > 0))
            return result
    raise last_exc


def _deliver_callback(submission, result, **retry_kwargs):
    callback_url = submission.get("callback_url")
    if not callback_url:
        return
    sid = submission["submission_id"]
    try:
        body = submission_status_body(sid, {"state": "DONE", "result": result})
        _retry(lambda: requests.post(callback_url, json=body, timeout=10).raise_for_status(),
               ledger=None, submission_id=sid, stage="callback", **retry_kwargs)
    except Exception as exc:
        print(f"[callback] delivery to {callback_url} failed for {sid}: {exc}", file=sys.stderr, flush=True)


def process_ingest_message(message, store, image_queue, ledger=None, skus=None, **retry_kwargs):
    """One ingest-queue message -> _stage_ingest, then hand off to image-queue (or
    finalize directly for a terminal Rejected/Clarification-required outcome).
    Idempotent: a submission already at state INGESTED or DONE is a no-op."""
    submission = message.body["submission"]
    sid = submission["submission_id"]
    skus = skus if skus is not None else set()

    state = store.get_submission_state(sid)
    if state is not None and state["state"] in ("INGESTED", "DONE"):
        return state  # already ingested (or fully done) -> nothing to redo
    # state == "RECEIVED" (the API's own pre-processing marker, or nothing at
    # all) both mean "not actually processed yet" -> fall through and do the work

    terminal, payload = _retry(
        lambda: pipeline._stage_ingest(submission, store, ledger, skus),
        ledger=ledger, submission_id=sid, stage="worker_ingest", **retry_kwargs)

    if terminal:
        pipeline._flush_cost_rows(store, ledger, sid)
        store.record_submission(sid, payload["status"], payload,
                                 user_id=submission.get("user_id"),
                                 parent_submission_id=submission.get("parent_submission_id"))
        _deliver_callback(submission, payload, **retry_kwargs)
    else:
        store.mark_submission_ingested(sid, payload["product_key"], payload["sku"],
                                        user_id=submission.get("user_id"),
                                        parent_submission_id=submission.get("parent_submission_id"))
        image_queue.send({"submission": submission, "product_key": payload["product_key"],
                           "sku": payload["sku"], "reused": payload["reused"]})
    return payload


def process_image_message(message, store, ledger=None, **retry_kwargs):
    """One image-queue message -> _stage_images, then finalize. Idempotent: a
    submission already DONE is a no-op (e.g. this job was redelivered after
    already completing)."""
    body = message.body
    submission = body["submission"]
    sid = submission["submission_id"]

    state = store.get_submission_state(sid)
    if state is not None and state["state"] == "DONE":
        return state["result"]

    result = _retry(
        lambda: pipeline._stage_images(submission, body["product_key"], body["sku"], body["reused"], store, ledger),
        ledger=ledger, submission_id=sid, stage="worker_images", **retry_kwargs)

    pipeline._flush_cost_rows(store, ledger, sid)
    store.record_submission(sid, result["status"], result,
                             user_id=submission.get("user_id"),
                             parent_submission_id=submission.get("parent_submission_id"))
    _deliver_callback(submission, result, **retry_kwargs)
    return result


def run_ingest_worker_once(store, ingest_queue, image_queue, ledger=None, skus=None,
                            max_messages=10, **retry_kwargs):
    """Drain whatever's currently in ingest-queue once, then return."""
    processed = []
    for message in ingest_queue.receive(max_messages=max_messages):
        try:
            processed.append(process_ingest_message(
                message, store, image_queue, ledger=ledger, skus=skus, **retry_kwargs))
        except Exception:
            continue  # exhausted retries -> leave for the queue's own redelivery/DLQ
        else:
            ingest_queue.delete(message.receipt_handle)  # ack only after success
    return processed


def run_image_worker_once(store, image_queue, ledger=None, max_messages=10, **retry_kwargs):
    """Drain whatever's currently in image-queue once, then return."""
    processed = []
    for message in image_queue.receive(max_messages=max_messages):
        try:
            processed.append(process_image_message(message, store, ledger=ledger, **retry_kwargs))
        except Exception:
            continue
        else:
            image_queue.delete(message.receipt_handle)
    return processed


def run_once(store, queues, ledger=None, skus=None, **retry_kwargs):
    """Drain both queues once: ingest first (which may enqueue image jobs this
    same pass), then image. Good enough for a single-process local demo;
    independent processes calling run_ingest_worker_once / run_image_worker_once
    on a loop is the real horizontally-scaled shape (System Design §7: separate
    CPU/GPU worker classes per queue)."""
    ingest_results = run_ingest_worker_once(
        store, queues["ingest"], queues["image"], ledger=ledger, skus=skus, **retry_kwargs)
    image_results = run_image_worker_once(store, queues["image"], ledger=ledger, **retry_kwargs)
    return ingest_results + image_results


def main():  # pragma: no cover - thin CLI wrapper, exercised via the functions above
    import argparse

    from .cost import CostLedger
    from .db import Store
    from .queue import connect_queues

    parser = argparse.ArgumentParser(description="Acme Resale ingest+image worker")
    parser.add_argument("--run-once", action="store_true",
                         help="drain whatever's currently queued once, then exit (local demo mode)")
    parser.add_argument("--loop", action="store_true",
                         help="poll the queues forever, draining each pass (local daemon mode); "
                              "Ctrl-C to stop")
    parser.add_argument("--interval", type=float, default=2.0,
                         help="seconds to sleep between passes in --loop mode (default: 2.0)")
    args = parser.parse_args()

    store = Store.connect()
    store.ensure_schema()
    queues = connect_queues()
    ledger = CostLedger()

    if args.run_once:
        results = run_once(store, queues, ledger=ledger)
        print(f"Processed {len(results)} submission(s). Cost this pass: ${ledger.total():.3f}")
        store.close()
        return

    if args.loop:
        import time

        print(f"Worker running — polling every {args.interval:.1f}s. Ctrl-C to stop.")
        try:
            while True:
                results = run_once(store, queues, ledger=ledger)
                if results:
                    print(f"Processed {len(results)} submission(s). "
                          f"Cost so far: ${ledger.total():.3f}")
                time.sleep(args.interval)
        except KeyboardInterrupt:
            print("\nStopping worker.")
        finally:
            store.close()
        return

    print("Choose a mode: --run-once (single drain) or --loop (local daemon). "
          "See --help.")
    store.close()


if __name__ == "__main__":  # pragma: no cover
    main()
