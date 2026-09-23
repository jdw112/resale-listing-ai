"""The {value, confidence, source} envelope — every field is wrapped so
blank-vs-unknown-vs-verified stays explicit and the gate is a data check."""

THRESHOLD = 0.60  # mandatory fields must meet this to pass the sufficiency gate

SOURCES = ("barcode", "label_ocr", "vision", "external_lookup",
           "context", "provided", "measured", "derived", "generated", "none")


def E(value, confidence, source):
    assert source in SOURCES, f"bad source: {source}"
    return {"value": value, "confidence": round(float(confidence), 2), "source": source}


def NULL():
    return {"value": None, "confidence": 0.0, "source": "none"}


def confident(env, threshold=THRESHOLD):
    """True only if the field has a real value AND meets the confidence bar."""
    if not isinstance(env, dict):
        return False
    return env.get("value") not in (None, "") and env.get("confidence", 0.0) >= threshold
