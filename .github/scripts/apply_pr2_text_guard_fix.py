from pathlib import Path


path = Path("aigateway-api/src/aigateway_api/dispatcher.py")
text = path.read_text(encoding="utf-8")
old = '''def _is_text_completion(body: Any) -> bool:
    """Return whether the request expects an assistant text response."""
    model = str(getattr(body, "model", "") or "").lower()
    if getattr(body, "generation_options", None) is not None:
        return False
    return not any(marker in model for marker in ("image", "video"))
'''
new = '''def _is_text_completion(body: Any) -> bool:
    """Return whether the request may produce an assistant text response.

    Generation options are routing hints, not a reliable modality signal. Auto
    requests can carry image/video controls and still be classified as text from
    the prompt, so only an explicitly media-named model bypasses text guards.
    """
    model = str(getattr(body, "model", "") or "").lower()
    return not any(marker in model for marker in ("image", "video"))
'''
if text.count(old) != 1:
    raise SystemExit(f"expected one text completion guard, found {text.count(old)}")
path.write_text(text.replace(old, new), encoding="utf-8")
