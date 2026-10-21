"""Recover user-uploaded visual context without confusing it with manual assets."""

import base64
import binascii


def latest_user_photo(history):
    """Read the latest upload in this session, even beyond the text-history window."""
    for message in reversed(history):
        if message.get("role") != "user" or not message.get("image"):
            continue
        image = message["image"]
        if not isinstance(image, str) or not image.startswith("data:image/png;base64,"):
            return None
        try:
            return base64.b64decode(image.split(",", 1)[1], validate=True)
        except (ValueError, binascii.Error):
            return None
    return None


def recent_context(history, text_limit=1600):
    recent = []
    for message in history[-6:]:
        text = message.get("text", "")
        if message.get("response"):
            text = "\n".join(
                block.get("content", "")
                for block in message["response"]["blocks"]
            )
        recent.append({
            "role": message["role"],
            "text": text[:text_limit],
            "has_user_photo": message.get("role") == "user" and bool(message.get("image")),
        })
    return recent
