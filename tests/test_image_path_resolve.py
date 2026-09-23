"""Regression test for the image-ref resolution bug: the intake API stores bare
filenames under the uploads root, but the worker's CWD is not that root, so the
adapters that read image bytes must resolve refs under UPLOADS_ROOT. Absolute
paths and already-existing relative paths (run_live_demo) are used unchanged."""

import importlib

import pytest


@pytest.fixture
def adapters(tmp_path, monkeypatch):
    monkeypatch.setenv("RESALE_LISTING_AI_UPLOADS_DIR", str(tmp_path))
    import resale_listing_ai.adapters as a
    importlib.reload(a)  # pick up the env-driven UPLOADS_ROOT
    yield a
    monkeypatch.delenv("RESALE_LISTING_AI_UPLOADS_DIR", raising=False)
    importlib.reload(a)


def test_bare_ref_resolves_under_uploads_root(adapters, tmp_path):
    (tmp_path / "abc123.jpg").write_bytes(b"x")
    assert adapters._resolve_image_path("abc123.jpg") == tmp_path / "abc123.jpg"


def test_absolute_path_used_as_is(adapters, tmp_path):
    other = tmp_path / "elsewhere.jpg"
    other.write_bytes(b"x")
    assert adapters._resolve_image_path(str(other)) == other


def test_existing_relative_path_used_as_is(adapters, tmp_path, monkeypatch):
    # run_live_demo passes e.g. uploads/photo1.jpg relative to CWD — if it exists
    # as given, don't rewrite it under the uploads root.
    monkeypatch.chdir(tmp_path)
    (tmp_path / "here.jpg").write_bytes(b"x")
    from pathlib import Path
    assert adapters._resolve_image_path("here.jpg") == Path("here.jpg")


def test_image_data_url_reads_from_uploads_root(adapters, tmp_path):
    from PIL import Image
    Image.new("RGB", (4, 4), "red").save(tmp_path / "pic.jpg")
    url = adapters._image_data_url("pic.jpg")  # bare ref
    assert url.startswith("data:image/jpeg;base64,")
