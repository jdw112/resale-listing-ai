"""Idempotently ensure the S3 bucket used for image storage exists (localstack
in dev via AWS_ENDPOINT_URL, real S3 in prod). Run from the container entrypoint
on startup. A failure here is non-fatal: the image pipeline's _connect_storage
still probes the bucket per submission and falls back to local disk when it is
unreachable — this just makes S3 the working default instead of always falling
back because the bucket was never created."""
import os
import sys

from resale_listing_ai import adapters


def ensure_bucket():
    """Create adapters.S3_BUCKET if it does not already exist. Returns True if the
    bucket exists (already or newly created), False on any error (logged, non-fatal)."""
    bucket = adapters.S3_BUCKET
    try:
        client = adapters._get_s3_client()
        try:
            client.head_bucket(Bucket=bucket)
            print(f"[bootstrap_s3] bucket {bucket!r} already exists", file=sys.stderr, flush=True)
            return True
        except Exception:
            pass

        region = os.environ.get("AWS_REGION", "ca-central-1")
        kwargs = {"Bucket": bucket}
        # Every region except us-east-1 requires an explicit LocationConstraint.
        if region != "us-east-1":
            kwargs["CreateBucketConfiguration"] = {"LocationConstraint": region}
        try:
            client.create_bucket(**kwargs)
            print(f"[bootstrap_s3] created bucket {bucket!r} in {region}", file=sys.stderr, flush=True)
        except Exception as exc:
            # A concurrent create (app + worker both boot this) is a success, not a failure.
            if exc.__class__.__name__ in ("BucketAlreadyOwnedByYou", "BucketAlreadyExists"):
                print(f"[bootstrap_s3] bucket {bucket!r} already exists (concurrent create)", file=sys.stderr, flush=True)
            else:
                raise
        return True
    except Exception as exc:
        print(f"[bootstrap_s3] could not ensure bucket {bucket!r} ({exc}); "
              f"image storage will fall back to local disk", file=sys.stderr, flush=True)
        return False


if __name__ == "__main__":
    ensure_bucket()
