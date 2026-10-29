# helper functions Retrieves previous conversation messages and the user's
#  latest uploaded photo to help the AI understand follow-up questions.

import base64
import binascii


# finds the user's latest uploaded photo in the chat, even if it is older than the recent messages.
# gives back None if there is no photo or it cannot be read
def latest_user_photo(history):
    """gets the latest photo the user uploaded in this session, even from old messages."""
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


# makes a short summary of the last 6 messages (who said it, the text, and if the user sent a photo).
# long text is cut to the text limit
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