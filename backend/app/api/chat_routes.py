import asyncio
import base64
import logging
import time
from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from google.auth.exceptions import RefreshError
from google.genai.errors import APIError
from pydantic import ValidationError
from app import config as cfg
from app.agent.chat_service import answer_question
from app.media.manual_images import normalize_image
from app.sessions.session_store import store

logger = logging.getLogger(__name__)
router = APIRouter()


@router.post("/chat")
async def chat(
    session_id: str = Form(...),
    message: str = Form(""),
    image: UploadFile | None = File(None),
):
    entry = await store.get(session_id)

    question = message.strip()
    photo = None

    if len(question) > 6000:
        raise HTTPException(
            400,
            "Please shorten the question "
            "to 6000 characters.",
        )

    if image:
        allowed_types = {
            "image/png",
            "image/jpeg",
            "image/webp",
        }

        if image.content_type not in allowed_types:
            raise HTTPException(
                400,
                "Use a PNG, JPEG or WebP image.",
            )

        data = await image.read(
            5 * 1024 * 1024 + 1
        )

        await image.close()

        if len(data) > 5 * 1024 * 1024:
            raise HTTPException(
                400,
                "The image must be 5 MB or smaller.",
            )

        try:
            photo = await asyncio.to_thread(
                normalize_image,
                data,
            )
        except Exception:
            raise HTTPException(
                400,
                "The uploaded image could not be read.",
            )

    if not question and not photo:
        raise HTTPException(
            400,
            "Enter a question or upload an image.",
        )

    async with entry.lock:
        await store.get(entry.id)

        entry.active = asyncio.current_task()
        started = time.monotonic()

        try:
            response, used_runner = (
                await asyncio.wait_for(
                    answer_question(
                        entry,
                        question,
                        photo,
                    ),
                    timeout=cfg.TIMEOUT,
                )
            )

            await store.get(entry.id)

            if not used_runner:
                await store.record_direct_reply(
                    entry,
                    question,
                    photo,
                    response,
                )

            user_message = {
                "role": "user",
                "text": question,
            }

            if photo:
                user_message["image"] = (
                    "data:image/png;base64,"
                    + base64.b64encode(
                        photo
                    ).decode()
                )

            entry.history.extend(
                [
                    user_message,
                    {
                        "role": "assistant",
                        "response": response,
                    },
                ]
            )

            return {
                **response,
                "session_id": entry.id,
                "expires_at": entry.expires_at,
                "elapsed_seconds": round(
                    time.monotonic() - started,
                    2,
                ),
            }

        except asyncio.TimeoutError:
            raise HTTPException(
                504,
                "The request timed out. "
                "Please try again.",
            )

        except asyncio.CancelledError:
            if entry.id not in store.entries:
                raise HTTPException(
                    410,
                    "Session ended or expired. "
                    "Start a new chat.",
                )

            raise

        except HTTPException:
            raise

        except RefreshError as exc:
            logger.exception("Google Cloud authentication failed")
            if "iam.serviceAccounts.getAccessToken" in str(exc):
                detail = (
                    "Google Cloud denied service-account impersonation. "
                    "Ask your Google Cloud administrator to grant the signed-in "
                    "account Service Account Token Creator on the configured "
                    "service account, then retry."
                )
            else:
                detail = (
                    "Google Cloud authentication failed. Refresh the backend's "
                    "application default credentials, then retry."
                )
            raise HTTPException(503, detail) from exc

        except APIError as exc:
            logger.exception("Model service request failed")
            if exc.code == 429:
                raise HTTPException(
                    503,
                    "Gemini is busy or its request limit was reached. "
                    "Please retry in a few seconds.",
                    headers={"Retry-After": "5"},
                ) from exc
            raise HTTPException(
                503, "The model service failed. Please retry."
            ) from exc

        except (ValidationError, ValueError):
            logger.exception(
                "Agent output validation failed"
            )

            raise HTTPException(
                502,
                "The agent returned an invalid "
                "evidence-grounded response. Please retry.",
            )

        except Exception:
            logger.exception(
                "Agent request failed"
            )

            raise HTTPException(
                503,
                "A model, BigQuery, or asset service failed. "
                "Check the backend terminal.",
            )

        finally:
            entry.active = None
