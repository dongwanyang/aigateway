"""Detection of anaphoric image references in generation prompts.

A video request that points at "this image" must name the image explicitly, via
an uploaded ``image_url`` part or a ``source_draft_id``. When the reference is
missing the request has to fail closed: silently generating a fresh keyframe
produces an unrelated subject, burns GPU time, and hides the mistake from the
user (progressive-video design plan, section 4.3).

Intent is *not* inferred here. Callers pass the already-resolved
``pipeline_kind`` so the fragile "is this asking for a video?" guesswork stays
out of this module; only the anaphora itself is matched.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

# Demonstratives and back-references that can only point at an image the user
# believes already exists. "上传/附" cover "上传的图" / "附件里的图片".
_ZH_DEIXIS = (
    "这|那|此|该|上述|上面|下面|前面|后面|之前|先前|刚才|刚刚|刚|"
    "原|原来|上一|前一|上传|附件|附|当前"
)
# Nouns deliberately exclude the bare "画面": in Chinese generation prompts it
# usually describes the scene still to be produced ("画面里有一只猫"), so
# matching it would reject legitimate text-to-video requests. Explicit result
# back-references are included because users often say "这个结果" after an image
# generation turn.
_ZH_IMAGE_NOUN = "图片|图像|照片|截图|影像|结果|图"

# Optional measure words / particles between the demonstrative and the noun,
# so "此图", "这张图片", "上面的图" and "刚才那一幅照片" all match.
_ZH_REFERENCE_RE = re.compile(
    rf"(?:{_ZH_DEIXIS})"
    rf"(?:个|张|幅|副|帧|条|份)?"
    rf"(?:这|那|此)?"
    rf"(?:一)?"
    rf"(?:个|张|幅|副|帧|条|份)?"
    rf"(?:的|里的|中的|上的)?"
    rf"\s*(?:{_ZH_IMAGE_NOUN})"
)

# Explicitly cover natural post-generation wording where the adjective itself
# carries the back-reference, for example "刚生成的图".
_ZH_GENERATED_REFERENCE_RE = re.compile(
    rf"刚(?:刚)?生成的\s*(?:{_ZH_IMAGE_NOUN})"
)

_EN_REFERENCE_RE = re.compile(
    r"\b(?:this|that|these|those|the\s+above|the\s+previous|previous|"
    r"the\s+last|last|the\s+attached|attached|the\s+uploaded|uploaded|"
    r"the\s+first|current|existing|latest|just[- ]generated|it)\s+"
    r"(?:\w+\s+){0,2}?"
    r"(?:images?|pictures?|photos?|photographs?|screenshots?|frames?|stills?|results?)\b",
    re.IGNORECASE,
)

_VIDEO_PIPELINE_KINDS = frozenset({"generation:video"})

REFERENCE_IMAGE_REQUIRED = "reference_image_required"
REFERENCE_IMAGE_REQUIRED_MESSAGE = (
    "未找到参考图片，请上传图片或从图片结果点击“基于此图生成视频”。"
)


def references_existing_image(text: str | None) -> bool:
    """Return whether the text points at an image the user expects to exist."""
    if not text:
        return False
    return bool(
        _ZH_REFERENCE_RE.search(text)
        or _ZH_GENERATED_REFERENCE_RE.search(text)
        or _EN_REFERENCE_RE.search(text)
    )


def _text_from_content(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, Sequence) and not isinstance(content, (str, bytes)):
        parts: list[str] = []
        for part in content:
            if not isinstance(part, Mapping):
                continue
            if part.get("type") != "text":
                continue
            value = part.get("text")
            if isinstance(value, str):
                parts.append(value)
        return "\n".join(parts)
    return ""


def latest_user_text(messages: Any) -> str:
    """Return the newest user turn's text without touching earlier history."""
    if not isinstance(messages, Sequence) or isinstance(messages, (str, bytes)):
        return ""
    for message in reversed(messages):
        if not isinstance(message, Mapping):
            continue
        if message.get("role") != "user":
            continue
        return _text_from_content(message.get("content"))
    return ""


def missing_required_image_reference(
    *,
    pipeline_kind: str,
    prompt_text: str | None,
    reference_image_urls: Iterable[str] | None = None,
    source_draft_id: str | None = None,
) -> bool:
    """Return whether a video request names an image it never supplied.

    Only ``generation:video`` is guarded. Image generation may legitimately
    mention a picture without attaching one, and understanding requests about an
    image are answered rather than rejected.
    """
    if pipeline_kind not in _VIDEO_PIPELINE_KINDS:
        return False
    if isinstance(source_draft_id, str) and source_draft_id.strip():
        return False
    for url in reference_image_urls or ():
        if isinstance(url, str) and url.strip():
            return False
    return references_existing_image(prompt_text)


__all__ = [
    "REFERENCE_IMAGE_REQUIRED",
    "REFERENCE_IMAGE_REQUIRED_MESSAGE",
    "latest_user_text",
    "missing_required_image_reference",
    "references_existing_image",
]
