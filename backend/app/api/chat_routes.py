

import asyncio
import base64
import logging
import time
from copy import deepcopy
from uuid import uuid4
from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, Request
from google.auth.exceptions import RefreshError
from google.genai.errors import APIError
from pydantic import ValidationError
from app import config as cfg
from app.agent.chat_service import answer_question
from app.media.manual_images import normalize_image
from app.sessions.session_store import store
from app.observability.timing import span, metadata, session_metadata, current_trace
from auth_system.security import require_approved, user_profile
from app.responses.schemas import RepairAction
from app.agent.safety import repair_intent, start_safety
from app.responses.documentation import create_document, public_document, queue_document_save
from app.agent.conversation_context import latest_user_photo

logger = logging.getLogger(__name__)
router = APIRouter()


@router.post("/chat")
async def chat(
    request: Request,
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
    issue_report: str = Form(''),
    plan_version: int = Form(1),
    observation: str = Form(''),
    safety_confirmed: bool = Form(False),
    start_new_case: bool = Form(False),
    identity=Depends(require_approved),
):
    # find the chat session
    request_started = time.perf_counter()
    session_metadata(session_id)
    metadata(question_length=len(message.strip()), has_image=image is not None, reused_image=False)
    with span('session_load'):
        entry = await store.owned(session_id, identity)

    # clean the question and prepare the image
    with span('input_validation'):
        question = message.strip()
        photo = None
        action = None
        if repair_action:
            try:
                action = RepairAction(action=repair_action, workflow_id=workflow_id, step_id=step_id,
                                      machine=machine, issue=issue, cause=cause,
                                      action_taken=action_taken, issue_report=issue_report,
                                      plan_version=plan_version, observation=observation,
                                      safety_confirmed=safety_confirmed)
            except ValidationError as exc:
                raise HTTPException(400, 'Invalid repair action or report details.') from exc
            if image and action.action != 'report_issue':
                raise HTTPException(400, 'Send repair confirmations separately from images.')
            question = ('Done — current repair step completed.' if action.action == 'done'
                        else ('The current repair step did not fix the issue. Technician report: '
                              + action.issue_report.strip() if action.action == 'report_issue'
                              else 'Issue resolved — create maintenance report.'))
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
        request_key = request.headers.get('X-Client-Request-ID', '')[:80] or str(uuid4())
        if request_key in entry.receipts:
            saved_response = entry.receipts[request_key]
            saved_workflow = saved_response.get('repair_workflow')
            current_workflow = entry.repair_workflow
            if saved_workflow and current_workflow and (
                    saved_workflow.get('plan_version', 1) != current_workflow.get('plan_version', 1)
                    or (saved_workflow.get('current_step') or {}).get('step_id') !=
                       (current_workflow.get('current_step') or {}).get('step_id')):
                raise HTTPException(409, 'That request belongs to an earlier step. Refresh the active case.')
            return saved_response
        if workflow_id and not action:
            current = entry.repair_workflow or {}
            if (current.get('workflow_id') != workflow_id or current.get('plan_version', 1) != plan_version
                    or (current.get('current_step') or {}).get('step_id', '') != step_id):
                raise HTTPException(409, 'The active step changed. Refresh this conversation before sending details.')
        if start_new_case and photo is None and len(entry.history) >= 2:
            previous_reply = entry.history[-1].get('response', {})
            if previous_reply.get('case_conflict') and previous_reply.get('new_case_question') == question:
                photo = latest_user_photo([entry.history[-2]])
                if photo:
                    metadata(reused_image=True)
        progress = {'status': 'processing', 'stage': 'Classifying your request', 'client_request_id': request_key}
        entry.progress[request_key] = progress
        if len(entry.progress) > 100:
            entry.progress.pop(next(iter(entry.progress)))
        entry.current_progress = progress
        entry.request_case_id = ''
        entry.safety_task = None
        previous_workflow = deepcopy(entry.repair_workflow)
        previous_cache = deepcopy(entry.repair_cache)
        previous_history_length = len(entry.history)
        previous_cases = deepcopy(entry.cases)
        if not action and repair_intent(question):
            start_safety(entry, question)

        if photo is not None:
            entry.uploaded_photo = photo

        # mark the request as running and start a timer
        entry.active = asyncio.current_task()
        started = time.monotonic()

        try:
            # get the agent's answer before the timeout
            response, used_runner = (
                await asyncio.wait_for(
                    answer_question(entry, question, photo, **({'repair_action': action} if action else {}),
                                    **({'start_new_case': True} if start_new_case else {})),
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
                'id': str(uuid4()),
                "role": "user",
                "text": question,
            }
            workflow = response.get('repair_workflow') or entry.repair_workflow
            case_id = '' if response.get('case_conflict') else (response.get('repair_workflow', {}).get('workflow_id') or getattr(entry, 'request_case_id', ''))
            if workflow and case_id:
                same_case = (previous_workflow or {}).get('workflow_id') == case_id
                user_message['case_id'] = case_id
                user_message['step_id'] = previous_workflow['current_step']['step_id'] if same_case and previous_workflow.get('current_step') else ''
                if action and action.action == 'report_issue':
                    response['plan_updated'] = True
                if not action and same_case and previous_workflow.get('current_step'):
                    workflow.setdefault('observations', []).append({
                        'step_id': previous_workflow['current_step']['step_id'], 'text': question,
                        'has_image': photo is not None, 'message_id': user_message['id']})

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
                assistant_id = str(uuid4())
                response['message_id'] = assistant_id
                response['request_type'] = (response.get('request_type') or
                    ('troubleshooting' if response.get('repair_workflow') else
                    'uploaded_image' if photo else 'manual_images' if any(b['type'] == 'image' for b in response.get('blocks', [])) else 'information'))
                response['session_id'] = entry.id
                entry.history.extend([user_message, {'id': assistant_id, 'case_id': case_id,
                                                     "role": "assistant", "response": deepcopy(response)}])
            if workflow and case_id:
                entry.repair_workflow = workflow
                entry.cases[case_id] = deepcopy(workflow)
            if action and action.action in ('resolve', 'unresolved'):
                with span('documentation_create'):
                    report = create_document(entry, identity, case_id=case_id)
                    profile = await asyncio.to_thread(user_profile, identity.uid) or {}
                    report['technician_name'] = profile.get('fullName', '')
                    queue_document_save(report, identity.uid)
                    response['documentation'] = public_document(report)
                    entry.history[-1]['response']['documentation'] = response['documentation']

            # send the answer and session details back
            result = {
                **response,
                "session_id": entry.id,
                "expires_at": entry.expires_at,
                "elapsed_seconds": round(
                    time.perf_counter() - request_started,
                    2,
                ),
                "request_id": current_trace.get().request_id if current_trace.get() else None,
            }
            entry.receipts[request_key] = result
            # Bound idempotency/progress memory without dropping active state.
            if len(entry.receipts) > 100:
                entry.receipts.pop(next(iter(entry.receipts)))
            # Firestore may need multiple writes for image-heavy sessions.
            # Queue a coalesced checkpoint so durable persistence does not hold
            # the completed answer in the request path.
            with span('checkpoint_queue'):
                await store.queue_checkpoint(entry)
            progress.update(status='completed', stage='Answer ready')
            return result

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
            if progress['status'] != 'completed':
                entry.repair_workflow = previous_workflow
                entry.repair_cache = previous_cache
                entry.history[previous_history_length:] = []
                entry.cases = previous_cases
                entry.receipts.pop(request_key, None)
                progress.update(status='error', stage='Request failed; you can retry the current step')
            if entry.safety_task and not entry.safety_task.done():
                entry.safety_task.cancel()
            entry.safety_task = None
            entry.current_progress = None
            entry.active = None
            entry.request_stage = ""
    finally:
        entry.lock.release()
