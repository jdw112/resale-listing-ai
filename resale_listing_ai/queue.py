"""Queue abstraction — the QUEUING PLANE of the System Design (§3, §8): an
ingest-queue and an image-queue, each with its own dead-letter queue, at-least-once
delivery, visibility timeout, and exponential-backoff-driven redelivery.

Two modes, switched by RESALE_LISTING_AI_QUEUE_MODE (env, or .env — see db._load_dotenv,
reused here so one .env file configures both the store and the queues):
  local (default) — in-process, in-memory; real visibility-timeout/receive-count/
          DLQ-redrive semantics (not a mocked stand-in for them), just no network
          or AWS account needed. What pytest/run_demo.py/--run-once use. Not
          persisted across process restarts — that durability is SQS's job in
          production, not this fallback's.
  sqs   — real Amazon SQS via boto3 (lazy-imported). Set AWS_ENDPOINT_URL to point
          the same client at LocalStack instead of real AWS for a local demo with
          the real SQS wire protocol.
"""

import json
import os
import time

from .db import _load_dotenv

# Above the slowest stage's realistic p95 (image processing: per-image classify +
# background-removal + optional cleanup + S3 puts, a handful of seconds each; a
# multi-image submission -> tens of seconds). Long jobs should heartbeat-extend
# visibility rather than risk redelivery mid-work; this default just needs to clear
# the common case with headroom.
DEFAULT_VISIBILITY_TIMEOUT = 300
DEFAULT_MAX_RECEIVE_COUNT = 3


class Message:
    def __init__(self, id, body, receipt_handle, receive_count):
        self.id = id
        self.body = body
        self.receipt_handle = receipt_handle
        self.receive_count = receive_count


def _queue_mode():
    return os.environ.get("RESALE_LISTING_AI_QUEUE_MODE", "local")


# ---------------------------------------------------------------------------
# LocalQueue — offline, in-memory, real at-least-once/visibility/DLQ semantics
# ---------------------------------------------------------------------------

class LocalQueue:
    def __init__(self, name, *, visibility_timeout=DEFAULT_VISIBILITY_TIMEOUT,
                 max_receive_count=DEFAULT_MAX_RECEIVE_COUNT, dlq=None, clock=time.monotonic):
        self.name = name
        self.visibility_timeout = visibility_timeout
        self.max_receive_count = max_receive_count
        self.dlq = dlq
        self._clock = clock
        self._messages = []
        self._next_id = 1

    def send(self, body):
        msg_id = str(self._next_id)
        self._next_id += 1
        self._messages.append({"id": msg_id, "body": body, "visible_at": 0.0, "receive_count": 0})
        return msg_id

    def receive(self, max_messages=1):
        now = self._clock()
        out = []
        for m in list(self._messages):
            if len(out) >= max_messages:
                break
            if m["visible_at"] > now:
                continue
            m["receive_count"] += 1
            if self.dlq is not None and m["receive_count"] > self.max_receive_count:
                self._messages.remove(m)
                self.dlq.send(m["body"])
                continue
            m["visible_at"] = now + self.visibility_timeout
            out.append(Message(m["id"], m["body"], m["id"], m["receive_count"]))
        return out

    def delete(self, receipt_handle):
        self._messages = [m for m in self._messages if m["id"] != receipt_handle]

    def __len__(self):
        return len(self._messages)


# ---------------------------------------------------------------------------
# SQSQueue — real Amazon SQS (or LocalStack via AWS_ENDPOINT_URL), via boto3
# ---------------------------------------------------------------------------

def _get_sqs_client():
    import boto3

    kwargs = {}
    endpoint = os.environ.get("AWS_ENDPOINT_URL")
    if endpoint:
        kwargs["endpoint_url"] = endpoint
    return boto3.client("sqs", region_name=os.environ.get("AWS_REGION", "ca-central-1"), **kwargs)


class SQSQueue:
    def __init__(self, client, queue_url, name):
        self._client = client
        self.queue_url = queue_url
        self.name = name

    @classmethod
    def declare(cls, name, *, visibility_timeout=DEFAULT_VISIBILITY_TIMEOUT,
                max_receive_count=DEFAULT_MAX_RECEIVE_COUNT, dlq=None, client=None):
        """Create (or reuse) the queue, wiring a redrive policy to `dlq` (another
        SQSQueue) if given — the DLQ itself is a plain queue with no redrive."""
        client = client or _get_sqs_client()
        attrs = {"VisibilityTimeout": str(visibility_timeout)}
        if dlq is not None:
            dlq_attrs = client.get_queue_attributes(QueueUrl=dlq.queue_url, AttributeNames=["QueueArn"])
            dlq_arn = dlq_attrs["Attributes"]["QueueArn"]
            attrs["RedrivePolicy"] = json.dumps(
                {"deadLetterTargetArn": dlq_arn, "maxReceiveCount": max_receive_count})
        resp = client.create_queue(QueueName=name, Attributes=attrs)
        return cls(client, resp["QueueUrl"], name)

    def send(self, body):
        resp = self._client.send_message(QueueUrl=self.queue_url, MessageBody=json.dumps(body))
        return resp["MessageId"]

    def receive(self, max_messages=1, wait_time_seconds=0):
        resp = self._client.receive_message(
            QueueUrl=self.queue_url, MaxNumberOfMessages=max_messages,
            WaitTimeSeconds=wait_time_seconds, AttributeNames=["ApproximateReceiveCount"],
        )
        out = []
        for m in resp.get("Messages", []):
            out.append(Message(
                id=m["MessageId"], body=json.loads(m["Body"]), receipt_handle=m["ReceiptHandle"],
                receive_count=int(m.get("Attributes", {}).get("ApproximateReceiveCount", 1)),
            ))
        return out

    def delete(self, receipt_handle):
        self._client.delete_message(QueueUrl=self.queue_url, ReceiptHandle=receipt_handle)

    def __len__(self):
        resp = self._client.get_queue_attributes(
            QueueUrl=self.queue_url, AttributeNames=["ApproximateNumberOfMessages"])
        return int(resp["Attributes"]["ApproximateNumberOfMessages"])


# ---------------------------------------------------------------------------
# Wiring both queue pairs (ingest + image), mode-selected
# ---------------------------------------------------------------------------

def _connect_one(name, *, mode, visibility_timeout, max_receive_count, dlq=None, client=None):
    if mode == "sqs":
        return SQSQueue.declare(
            name, visibility_timeout=visibility_timeout, max_receive_count=max_receive_count,
            dlq=dlq, client=client)
    return LocalQueue(name, visibility_timeout=visibility_timeout, max_receive_count=max_receive_count, dlq=dlq)


def connect_queues(mode=None, *, visibility_timeout=DEFAULT_VISIBILITY_TIMEOUT,
                    max_receive_count=DEFAULT_MAX_RECEIVE_COUNT):
    """The full queuing plane: ingest-queue + its DLQ, image-queue + its DLQ."""
    _load_dotenv()
    mode = mode or _queue_mode()
    client = _get_sqs_client() if mode == "sqs" else None

    ingest_dlq = _connect_one("ingest-dlq", mode=mode, visibility_timeout=visibility_timeout,
                               max_receive_count=max_receive_count, client=client)
    ingest = _connect_one("ingest-queue", mode=mode, visibility_timeout=visibility_timeout,
                           max_receive_count=max_receive_count, dlq=ingest_dlq, client=client)
    image_dlq = _connect_one("image-dlq", mode=mode, visibility_timeout=visibility_timeout,
                              max_receive_count=max_receive_count, client=client)
    image = _connect_one("image-queue", mode=mode, visibility_timeout=visibility_timeout,
                          max_receive_count=max_receive_count, dlq=image_dlq, client=client)
    return {"ingest": ingest, "ingest_dlq": ingest_dlq, "image": image, "image_dlq": image_dlq}
