"""Tests for resale_listing_ai/bootstrap_s3.py — idempotent S3 bucket creation."""


class _FakeS3:
    def __init__(self, exists):
        self._exists = exists
        self.create_kwargs = None

    def head_bucket(self, Bucket):
        if not self._exists:
            raise RuntimeError("404 Not Found")

    def create_bucket(self, **kwargs):
        self.create_kwargs = kwargs


def test_ensure_bucket_creates_when_missing(monkeypatch):
    from resale_listing_ai import adapters, bootstrap_s3

    fake = _FakeS3(exists=False)
    monkeypatch.setattr(adapters, "_get_s3_client", lambda: fake)
    monkeypatch.setenv("AWS_REGION", "ca-central-1")

    assert bootstrap_s3.ensure_bucket() is True
    assert fake.create_kwargs["Bucket"] == adapters.S3_BUCKET
    assert fake.create_kwargs["CreateBucketConfiguration"]["LocationConstraint"] == "ca-central-1"


def test_ensure_bucket_skips_create_when_present(monkeypatch):
    from resale_listing_ai import adapters, bootstrap_s3

    fake = _FakeS3(exists=True)
    monkeypatch.setattr(adapters, "_get_s3_client", lambda: fake)

    assert bootstrap_s3.ensure_bucket() is True
    assert fake.create_kwargs is None  # existing bucket -> no create call


def test_ensure_bucket_us_east_1_omits_location_constraint(monkeypatch):
    from resale_listing_ai import adapters, bootstrap_s3

    fake = _FakeS3(exists=False)
    monkeypatch.setattr(adapters, "_get_s3_client", lambda: fake)
    monkeypatch.setenv("AWS_REGION", "us-east-1")

    assert bootstrap_s3.ensure_bucket() is True
    assert "CreateBucketConfiguration" not in fake.create_kwargs


def test_ensure_bucket_non_fatal_on_error(monkeypatch):
    from resale_listing_ai import adapters, bootstrap_s3

    def boom():
        raise RuntimeError("boto3 unavailable")

    monkeypatch.setattr(adapters, "_get_s3_client", boom)

    assert bootstrap_s3.ensure_bucket() is False  # never raises out of startup
