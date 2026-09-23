"""Tests for resale_listing_ai/queue.py's SQSQueue — the real production path — using
moto's in-process AWS mock (@mock_aws). This exercises the actual boto3 SQS calls
(create_queue, send_message, receive_message with a real visibility timeout,
delete_message, redrive policy) against a real implementation of the SQS API
surface, not a hand-rolled Python stand-in — and needs no Docker/LocalStack/network,
so it runs by default alongside the rest of the suite.
"""

import time

import pytest

moto = pytest.importorskip("moto")
from moto import mock_aws  # noqa: E402


@mock_aws
def test_sqs_send_then_receive_returns_the_message_body():
    from resale_listing_ai.queue import SQSQueue

    q = SQSQueue.declare("test-ingest-queue")
    q.send({"submission_id": "s1"})

    [msg] = q.receive()

    assert msg.body == {"submission_id": "s1"}


@mock_aws
def test_sqs_delete_acks_and_removes_the_message():
    from resale_listing_ai.queue import SQSQueue

    q = SQSQueue.declare("test-ingest-queue")
    q.send({"submission_id": "s1"})
    [msg] = q.receive()

    q.delete(msg.receipt_handle)

    assert len(q) == 0


@mock_aws
def test_sqs_received_message_is_hidden_until_visibility_timeout_expires():
    from resale_listing_ai.queue import SQSQueue

    q = SQSQueue.declare("test-ingest-queue", visibility_timeout=1)
    q.send({"submission_id": "s1"})

    first = q.receive()
    assert len(first) == 1
    assert q.receive() == []  # still hidden

    time.sleep(1.5)
    redelivered = q.receive()
    assert len(redelivered) == 1


@mock_aws
def test_sqs_receive_count_increments_on_redelivery():
    from resale_listing_ai.queue import SQSQueue

    q = SQSQueue.declare("test-ingest-queue", visibility_timeout=1)
    q.send({"submission_id": "s1"})

    [first] = q.receive()
    assert first.receive_count == 1

    time.sleep(1.5)
    [second] = q.receive()
    assert second.receive_count == 2


@mock_aws
def test_sqs_exceeding_max_receive_count_redrives_to_dlq():
    from resale_listing_ai.queue import SQSQueue

    dlq = SQSQueue.declare("test-ingest-dlq")
    q = SQSQueue.declare("test-ingest-queue", visibility_timeout=1, max_receive_count=2, dlq=dlq)
    q.send({"submission_id": "poison"})

    q.receive()               # attempt 1
    time.sleep(1.5)
    q.receive()                # attempt 2
    time.sleep(1.5)
    third = q.receive()        # attempt 3 -> SQS itself redrives to the DLQ

    assert third == []
    [dead] = dlq.receive()
    assert dead.body == {"submission_id": "poison"}


@mock_aws
def test_connect_queues_wires_all_four_sqs_queues(monkeypatch):
    from resale_listing_ai.queue import connect_queues

    monkeypatch.setenv("RESALE_LISTING_AI_QUEUE_MODE", "sqs")

    queues = connect_queues()

    assert set(queues) == {"ingest", "ingest_dlq", "image", "image_dlq"}
    for q in queues.values():
        assert len(q) == 0  # freshly declared, empty
