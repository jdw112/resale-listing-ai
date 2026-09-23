"""Small helpers shared by the provider adapters in this package. Deliberately
dependency-free (stdlib only) so importing it never pulls in a provider SDK."""

import mimetypes


def guess_mime_type(image_path):
    """MIME type for an image path, defaulting to image/jpeg when the extension
    is unknown — the shape every provider that sends a raw image + its declared
    MIME type needs (gemini.py's Part.from_bytes, openrouter.py's data: URL).

    NOTE: this trusts the file EXTENSION. Providers with a fixed accepted-format
    list (Bedrock's Converse API) must not rely on it — see bedrock.py's
    _image_block, which decodes the real content instead."""
    return mimetypes.guess_type(str(image_path))[0] or "image/jpeg"
