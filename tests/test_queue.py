"""Tests for resale_listing_ai/queue.py's LocalQueue — the offline fallback used by
pytest/run_demo.py/--run-once (RESALE_LISTING_AI_QUEUE_MODE=local, the default). Real
SQS-mode coverage (via moto, no AWS/LocalStack needed) lives in test_queue_sqs.py.

Visibility-timeout/redelivery timing is driven by an injectable `clock` (a plain
callable returning a float, like time.monotonic) so these tests are deterministic
and instant — no real sleeping.
"""


def test_send_then_receive_returns_the_message_body():
    from resale_listing_ai.queue import LocalQueue

    q = LocalQueue("ingest-queue")
    q.send({"submission_id": "s1"})

    [msg] = q.receive()

    assert msg.body == {"submission_id": "s1"}


def test_receive_with_nothing_queued_returns_empty():
    from resale_listing_ai.queue import LocalQueue

    q = LocalQueue("ingest-queue")

    assert q.receive() == []


def test_received_message_is_hidden_until_visibility_timeout_expires():
    from resale_listing_ai.queue import LocalQueue

    now = [0.0]
    q = LocalQueue("ingest-queue", visibility_timeout=30, clock=lambda: now[0])
    q.send({"submission_id": "s1"})

    first = q.receive()
    assert len(first) == 1

    now[0] += 10  # well within the 30s visibility window
    assert q.receive() == []  # still hidden -> at-least-once, not at-least-twice-early


def test_message_becomes_visible_again_after_visibility_timeout_expires():
    """Simulates a worker crashing mid-processing: nothing ever deleted it, so once
    the visibility timeout lapses it's redelivered — the at-least-once guarantee."""
    from resale_listing_ai.queue import LocalQueue

    now = [0.0]
    q = LocalQueue("ingest-queue", visibility_timeout=30, clock=lambda: now[0])
    q.send({"submission_id": "s1"})
    q.receive()

    now[0] += 31
    redelivered = q.receive()

    assert len(redelivered) == 1
    assert redelivered[0].body == {"submission_id": "s1"}


def test_delete_acks_and_permanently_removes_the_message():
    from resale_listing_ai.queue import LocalQueue

    now = [0.0]
    q = LocalQueue("ingest-queue", visibility_timeout=30, clock=lambda: now[0])
    q.send({"submission_id": "s1"})
    [msg] = q.receive()

    q.delete(msg.receipt_handle)

    now[0] += 100  # long past visibility timeout -> would have redelivered if not acked
    assert q.receive() == []
    assert len(q) == 0


def test_receive_count_increments_on_each_redelivery():
    from resale_listing_ai.queue import LocalQueue

    now = [0.0]
    q = LocalQueue("ingest-queue", visibility_timeout=10, clock=lambda: now[0])
    q.send({"submission_id": "s1"})

    [first] = q.receive()
    assert first.receive_count == 1

    now[0] += 11
    [second] = q.receive()
    assert second.receive_count == 2


def test_exceeding_max_receive_count_redrives_to_dlq_instead_of_redelivering():
    from resale_listing_ai.queue import LocalQueue

    now = [0.0]
    dlq = LocalQueue("ingest-dlq", clock=lambda: now[0])
    q = LocalQueue("ingest-queue", visibility_timeout=5, max_receive_count=2, dlq=dlq, clock=lambda: now[0])
    q.send({"submission_id": "poison"})

    q.receive()          # attempt 1
    now[0] += 6
    q.receive()           # attempt 2 (== max_receive_count, still delivered)
    now[0] += 6
    third = q.receive()   # attempt 3 (> max_receive_count) -> redriven to DLQ, not redelivered

    assert third == []
    assert len(q) == 0
    [dead] = dlq.receive()
    assert dead.body == {"submission_id": "poison"}


def test_receive_respects_max_messages_limit():
    from resale_listing_ai.queue import LocalQueue

    q = LocalQueue("ingest-queue")
    for i in range(5):
        q.send({"submission_id": f"s{i}"})

    batch = q.receive(max_messages=2)

    assert len(batch) == 2


def test_len_reflects_current_queue_depth_not_in_flight_hidden_count():
    """__len__ counts everything still IN the queue (including messages currently
    checked out / hidden by visibility timeout) — deleted/DLQ'd messages don't
    count; this is queue depth for observability, not "ready to receive right now"."""
    from resale_listing_ai.queue import LocalQueue

    q = LocalQueue("ingest-queue")
    q.send({"submission_id": "s1"})
    q.send({"submission_id": "s2"})
    q.receive(max_messages=1)  # one now hidden, still enqueued

    assert len(q) == 2
