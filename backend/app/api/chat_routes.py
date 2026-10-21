# handles chat requests from the frontend and returns the agent's answer

import asyncio
import base64
import logging
import time
from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from google.auth.exceptions import RefreshError
from google.genai.errors import APIError
from pydantic import ValidationError
from app import config as cfg
from app.agent.chat_service import answer_question
from app.media.manual_images import normalize_image
from app.sessions.session_store import store
from app.observability.timing import span, metadata, session_metadata, current_trace
from auth_system.security import require_approved
from app.responses.schemas import RepairAction

logger = logging.getLogger(__name__)
router = APIRouter()


@router.post("/chat")
async def chat(
    session_id: str = Form(...),
    message: str = Form(""),
    image: UploadFile | None = File(None),
    repair_action: str = Form(''),
    workflow_id: str = Form(''),
    step_id: str = Form(''),
    machine: str = Form(''),
    issue: str = Form(''),
    cause: str = Form(''),
    action_taken: str = Form(''),
    identity=Depends(require_approved),
):
    # find the chat session
    request_started = time.perf_counter()
    session_metadata(session_id)
    metadata(question_length=len(message.strip()), has_image=image is not None, reused_image=False)
    with span('session_load'):
        entry = await store.get(session_id)

    # clean the question and prepare the image
    with span('input_validation'):
        question = message.strip()
        photo = None
        action = None
        if repair_action:
            try:
                action = RepairAction(action=repair_action, workflow_id=workflow_id, step_id=step_id,
                                      machine=machine, issue=issue, cause=cause, action_taken=action_taken)
            except ValidationError as exc:
                raise HTTPException(400, 'Invalid repair action or report details.') from exc
            if image:
                raise HTTPException(400, 'Send repair confirmations separately from images.')
            question = ('Done — current repair step completed.' if action.action == 'done'
                        else 'Issue resolved — create maintenance report.')
        if len(question) > 6000:
            raise HTTPException(400, 'Please shorten the question to 6000 characters.')

    # check and read the uploaded image
    if image:
        allowed_types = {
            "image/png",
            "image/jpeg",
            "image/webp",
        }

        # allow only png, jpeg, or webp
        if image.content_type not in allowed_types:
            raise HTTPException(
                400,
                "Use a PNG, JPEG or WebP image.",
            )

        # read the image and close the file
        with span('upload_read'):
            data = await image.read(5 * 1024 * 1024 + 1)
        metadata(image_size_bytes=len(data))
        await image.close()

        # check that the image is no larger than 5 mb
        if len(data) > 5 * 1024 * 1024:
            raise HTTPException(
                400,
                "The image must be 5 MB or smaller.",
            )

        # prepare the image for the agent
        try:
            with span('upload_normalization'):
                photo = await asyncio.to_thread(normalize_image, data)
            metadata(normalized_image_bytes=len(photo))
        except Exception:
            raise HTTPException(
                400,
                "The uploaded image could not be read.",
            )

    # make sure the user sent a question or image
    if not question and not photo and not action:
        raise HTTPException(
            400,
            "Enter a question or upload an image.",
        )

    # handle one request at a time for this chat
    with span('session_lock_wait'):
        await entry.lock.acquire()
    try:
        # check that the session still exists
        await store.get(entry.id)

        if photo is not None:
            entry.uploaded_photo = photo

        # mark the request as running and start a timer
        entry.active = asyncio.current_task()
        started = time.monotonic()

        try:
            # get the agent's answer before the timeout
            response, used_runner = (
                await asyncio.wait_for(
                    answer_question(entry, question, photo, **({'repair_action': action} if action else {})),
                    timeout=cfg.TIMEOUT,
                )
            )

            # check that the session still exists
            await store.get(entry.id)

            # save the reply if the runner did not save it
            if not used_runner:
                with span('direct_reply_save'):
                    await store.record_direct_reply(entry, question, photo, response)

            # prepare the user's message for chat history
            user_message = {
                "role": "user",
                "text": question,
            }

            # add the image to chat history
            if photo:
                user_message["image"] = (
                    "data:image/png;base64,"
                    + base64.b64encode(
                        photo
                    ).decode()
                )

            # save the question and answer
            with span('history_save'):
                entry.history.extend([user_message, {"role": "assistant", "response": response}])

            # send the answer and session details back
            return {
                **response,
                "session_id": entry.id,
                "expires_at": entry.expires_at,
                "elapsed_seconds": round(
                    time.perf_counter() - request_started,
                    2,
                ),
                "request_id": current_trace.get().request_id if current_trace.get() else None,
            }

        # tell the user if the request takes too long
        except asyncio.TimeoutError as exc:
            metadata(error_type=type(exc).__name__)
            stage = entry.request_stage or "answer preparation"
            logger.warning("Chat timed out during %s after %.2fs", stage, time.monotonic() - started)
            raise HTTPException(
                504,
                f"The request timed out during {stage}. Please try again.",
            )

        # handle a request cancelled because the session ended
        except asyncio.CancelledError:
            if entry.id not in store.entries:
                raise HTTPException(
                    410,
                    "Session ended or expired. "
                    "Start a new chat.",
                )

            raise

        # pass through http errors
        except HTTPException:
            raise

        # handle google cloud login errors
        except RefreshError as exc:
            metadata(error_type=type(exc).__name__)
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

        # handle gemini errors
        except APIError as exc:
            metadata(error_type=type(exc).__name__)
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

        # handle an invalid answer from the agent
        except (ValidationError, ValueError) as exc:
            metadata(error_type=type(exc).__name__)
            logger.exception(
                "Agent output validation failed"
            )

            raise HTTPException(
                502,
                "The agent returned an invalid "
                "evidence-grounded response. Please retry.",
            )

        # handle other errors
        except Exception as exc:
            metadata(error_type=type(exc).__name__)
            logger.exception(
                "Agent request failed"
            )

            raise HTTPException(
                503,
                "A model, BigQuery, or asset service failed. "
                "Check the backend terminal.",
            )

        # mark the request as finished
        finally:
            entry.active = None
            entry.request_stage = ""
    finally:
        entry.lock.release()
