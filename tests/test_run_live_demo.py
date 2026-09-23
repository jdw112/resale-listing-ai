"""Smoke test for run_live_demo.py — the real-provider 1-2 image demo script.
Only checks the no-API-keys-needed usage path (matches run_demo.py's own
precedent of not being pytest-imported/unit-tested beyond a basic smoke
check); the real-provider path needs GEMINI_API_KEY/OPENROUTER_API_KEY and
real photos, which is exactly what this script is for running manually."""

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_run_live_demo_prints_usage_and_exits_nonzero_with_no_images():
    result = subprocess.run(
        [sys.executable, str(ROOT / "run_live_demo.py")],
        capture_output=True, text=True, cwd=ROOT,
    )

    assert result.returncode != 0
    assert "Usage:" in result.stdout


def test_run_live_demo_rejects_a_nonexistent_image_path():
    result = subprocess.run(
        [sys.executable, str(ROOT / "run_live_demo.py"), "/no/such/file.jpg"],
        capture_output=True, text=True, cwd=ROOT,
    )

    assert result.returncode != 0
    assert "Not a file" in result.stdout


def test_run_live_demo_catches_an_env_var_passed_as_an_argument():
    """A common real mistake: RESALE_LISTING_AI_MAX_IMAGES=5 typed after the command
    instead of before it, which argv sees as just another path argument.
    This must get a specific, actionable message -- not the generic
    "Not a file" a stray non-existent path would otherwise produce."""
    result = subprocess.run(
        [sys.executable, str(ROOT / "run_live_demo.py"), "uploads", "RESALE_LISTING_AI_MAX_IMAGES=5"],
        capture_output=True, text=True, cwd=ROOT,
    )

    assert result.returncode != 0
    assert "environment variable" in result.stdout
    assert "RESALE_LISTING_AI_MAX_IMAGES=5" in result.stdout
    assert "Not a file" not in result.stdout


def _fake_record(fields):
    """A minimal envelope-shaped record: every printed field must be present."""
    return {f: {"value": None, "confidence": 0.0, "source": "none"} for f in fields}


def _drive_main(monkeypatch, capsys, *, reused):
    """Run run_live_demo.main() against a fake store/_stage_ingest so the reuse
    warning can be exercised without API keys or a live database."""
    import run_live_demo

    product_fields = ("Brand", "ProductName", "ModelNumber", "Variant", "Colour",
                      "InternalCategory", "PricingAnchorPriceCAD", "PricingAnchorSourceURL",
                      "ComparableProductPriceCAD", "ComparableProductURL",
                      "MasterTitle", "ShortTitle", "MasterDescription", "HeroImageURL")
    listing_fields = ("ProvidedConditionGrade", "OriginalPhotoFolderURL", "ScrubbedPhotoFolderURL")

    class _FakeStore:
        def ensure_schema(self):
            pass

        def get_product(self, key):
            return _fake_record(product_fields)

        def get_listing(self, sku):
            return _fake_record(listing_fields)

        def close(self):
            pass

    monkeypatch.setattr(run_live_demo.Store, "connect", classmethod(lambda cls: _FakeStore()))
    monkeypatch.setattr(
        run_live_demo, "_stage_ingest",
        lambda submission, store, ledger, skus: (
            False, {"product_key": "ACME-X-ASH", "sku": "RL-TEST-001", "reused": reused}))
    monkeypatch.setattr(
        run_live_demo, "_stage_images",
        lambda submission, product_key, sku, reused, store, ledger: {
            "status": "Accepted with unknowns",
            "product": store.get_product(product_key),
            "listing": store.get_listing(sku),
        })
    monkeypatch.setattr(sys, "argv", ["run_live_demo.py", str(Path(__file__))])

    run_live_demo.main()
    return capsys.readouterr().out


def test_warns_loudly_when_the_product_key_was_reused(monkeypatch, capsys):
    """Without this warning the script prints the FIRST provider's stored pricing
    and copy under the SECOND provider's banner — silently invalidating the whole
    point of the side-by-side comparison."""
    out = _drive_main(monkeypatch, capsys, reused=True)

    assert "WARNING" in out
    assert "ProductKey reused" in out
    assert "NOT freshly generated" in out
    # the warning must precede the pricing/copy it is warning about
    assert out.index("WARNING") < out.index("Pricing:")


def test_no_reuse_warning_on_a_genuinely_new_product(monkeypatch, capsys):
    out = _drive_main(monkeypatch, capsys, reused=False)

    assert "WARNING" not in out
    assert "ProductKey reused" not in out
