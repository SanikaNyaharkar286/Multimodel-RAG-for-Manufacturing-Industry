# this file searches the manuals and uses the agent to answer chat questions


import asyncio
import json
import logging
import re
import time
from google.adk.agents.run_config import RunConfig
from google.adk.runners import Runner
from google.genai import types
from app import config as cfg
from app.agent.agent import root_agent
from app.agent.planner import understand
from app.media.prepare_media import prepare_media
from app.responses.response_builder import text_response, final_blocks
from app.responses.schemas import AgentAnswer
from app.retrieval.manual_search import list_available_documents, search_manual
from app.sessions.session_store import session_service, store
from .conversation_context import latest_user_photo, recent_context
from .inventory import is_inventory_request, inventory_response
from app.observability.timing import span, metadata, usage_metadata, increment, current_trace


# connects the agent to the session service
runner = Runner(agent=root_agent, app_name=cfg.APP_NAME, session_service=session_service)
logger = logging.getLogger("uvicorn.error")

# Keep model requests small while retaining the complete retrieval result for
# response rendering and source links.
# Keep the final multimodal request small enough for predictable latency.
MAX_MODEL_EVIDENCE = 8
MAX_MODEL_IMAGES = 6
MAX_MODEL_IMAGE_BYTES = 1_500_000


def _explicit_follow_up_request(question):
    """Return true only for wording that clearly asks to continue a prior task."""
    text = re.sub(r"[^a-z0-9 ]+", " ", question.lower())
    return bool(re.search(
        r"\b(next step|next|continue|what should i check now|what do i check next|why that|why this check|then what)\b",
        text,
    ))


# plans the search and gets an answer for the user's question
async def answer_question(entry, question, photo):
    async def stage(name, function, *args):
        entry.request_stage = name
        started = time.monotonic()
        logger.info("Chat stage started: %s", name)
        try:
            key = {'manual inventory lookup': 'inventory',
                   'image and question analysis': 'planner', 'question analysis': 'planner',
                   'manual evidence retrieval': 'retrieval',
                   'manual image and link loading': 'media'}[name]
            with span(key):
                return await asyncio.to_thread(function, *args)
        finally:
            logger.info("Chat stage ended: %s (%.2fs)", name, time.monotonic() - started)

    # get the saved details for this session
    state = await store.state(entry)

    async def answer_inventory():
        documents = await stage("manual inventory lookup", list_available_documents)
        metadata(outcome='inventory')
        return text_response(inventory_response(documents)), False

    # Common catalog requests need a full grouped query, not ranked manual text.
    if photo is None and is_inventory_request(question):
        return await answer_inventory()

    # understand what the user is asking
    previous_photo = (
        getattr(entry, "uploaded_photo", None) or latest_user_photo(entry.history)
    ) if photo is None else None
    plan = await stage(
        "image and question analysis" if photo or previous_photo else "question analysis",
        understand,
        question,
        state,
        entry.history,
        photo,
        previous_photo,
    )

    # A new upload normally starts a new subject. Do not let the planner carry
    # the previous topic into it unless the wording explicitly asks to continue.
    if photo is not None and plan.follow_up and not _explicit_follow_up_request(question):
        plan.follow_up = False
        plan.use_previous_photo = False
        plan.advance = False
        plan.guided = False

    # The planner handles catalog paraphrases outside the local fast path.
    if plan.intent == 'document_inventory':
        return await answer_inventory()

    # Only carry the earlier upload into generation when the planner selected it.
    using_previous_photo = photo is None and plan.use_previous_photo and previous_photo is not None
    if using_previous_photo:
        photo = previous_photo
        metadata(reused_image=True)

    # reply if the requested machine is not in the manuals
    if plan.model_mismatch:
        metadata(outcome='model_mismatch')
        response = text_response(
            "- The requested machine is not present in the indexed manuals.\n"
            "- Please confirm the machine model or provide its matching manual."
        )

        return response, False

    # ask the user for more details if needed
    if plan.clarify:
        metadata(outcome='clarification')
        return text_response(plan.clarify), False

    # reply if the question is outside the app's scope
    if plan.out_of_scope:
        metadata(outcome='out_of_scope')
        return text_response(cfg.UNAVAILABLE), False

    # keep the same topic for a follow-up question
    if plan.follow_up and not using_previous_photo:
        topic = state.get("main_topic", "")
    else:
        topic = plan.topic

    # choose a topic or search query
    topic = topic or plan.topic or plan.query

    # keep guided steps active during follow-up questions
    guided = (
        plan.guided
        or (
            plan.follow_up
            and state.get("guided", False)
        )
    )

    # get the previous step number for a follow-up
    current_step = (
        state.get("step_number", 0)
        if plan.follow_up
        else 0
    )

    # work out which guided step to show
    if guided:
        step_increment = 1 if plan.advance else 0
        step = max(1, current_step + step_increment)
    else:
        step = 0

    # choose the text to search for in the manuals
    query_text = (
        plan.query
        or topic
        or question
    )

    # include the main topic in a follow-up search if needed
    if (
        plan.follow_up
        and topic.lower() not in query_text.lower()
    ):
        query_text = f"{topic}. {query_text}"

    # search the manuals for matching information
    bundle = await stage(
        "manual evidence retrieval",
        search_manual,
        query_text,
        plan.wants_visual,
        plan.wants_video,
    )

    # reply if no matching manual information was found
    if not bundle["chunks"]:
        metadata(outcome='unsupported', chunk_count=0)
        return text_response(cfg.UNAVAILABLE), False

    # prepare related images and links
    logger.info("Retrieved %d chunks, %d image references and %d links",
                len(bundle["chunks"]), len(bundle["images"]), len(bundle["links"]))
    media = await stage(
        "manual image and link loading",
        prepare_media,
        bundle,
        plan.wants_visual,
        plan.wants_video,
    )

    logger.info("Prepared %d images and %d links; %d asset warnings",
                len(media["images"]), len(media["links"]), len(media["warnings"]))
    metadata(chunk_count=len(bundle['chunks']), response_image_count=len(media['images']),
             response_link_count=len(media['links']), asset_warning_count=len(media['warnings']))

    # collect the current topic and answer settings
    payload_started = time.perf_counter()
    workflow = {
        "MAIN_TOPIC": topic,
        "GUIDED": guided,
        "STEP_NUMBER": step,
        "ADVANCE": plan.advance,
        "FOLLOW_UP": plan.follow_up,
        "CURRENT_STEP_ALREADY_GIVEN": bool(current_step),
        "wants_sources": plan.wants_sources,
        "wants_visual": plan.wants_visual,
        "wants_video": plan.wants_video,
    }

    # prepare recent messages to give the agent context
    recent = recent_context(entry.history, text_limit=1800)

    # Give the model only the highest-ranked evidence. The full bundle remains
    # available below for response rendering and validation.
    model_chunks = bundle["chunks"][:MAX_MODEL_EVIDENCE]

    # give the evidence short ids for this request
    evidence_ids = {
        row["chunk_id"]: f"evidence-{index + 1}"
        for index, row in enumerate(model_chunks)
    }
    source_ids = {value: key for key, value in evidence_ids.items()}

    # Add only relevant images, up to a bounded payload budget. The uploaded
    # user photo is handled separately and is never counted against this list.
    model_images = []
    model_image_bytes = 0
    for image in media["images"]:
        if image["chunk_id"] not in evidence_ids or len(model_images) >= MAX_MODEL_IMAGES:
            continue
        image_size = len(image["bytes"])
        if model_images and model_image_bytes + image_size > MAX_MODEL_IMAGE_BYTES:
            continue
        model_images.append(image)
        model_image_bytes += image_size

    image_information = [
        {
            **{key: value for key, value in image.items() if key != "bytes"},
            "chunk_id": evidence_ids[image["chunk_id"]],
        }
        for image in model_images
    ]

    # collect the question and approved manual information
    payload = {
        "question": question,
        "resolved_query": query_text,
        "user_photo_origin": "previous_upload" if using_previous_photo else (
            "current_upload" if photo is not None else "none"
        ),
        "workflow": workflow,
        "recent_context": recent,
        "APPROVED_EVIDENCE": [
            {**row, "chunk_id": evidence_ids[row["chunk_id"]]}
            for row in model_chunks
        ],
        "allowed_images": image_information,
        "allowed_links": [
            {**link, "chunk_id": evidence_ids[link["chunk_id"]]}
            for link in media["links"]
            if link["chunk_id"] in evidence_ids
        ],
        "asset_load_warnings": media["warnings"],
    }

    # turn the information into a message for the agent
    parts = [
        types.Part(
            text=json.dumps(
                payload,
                ensure_ascii=False,
            )
        )
    ]

    # add the user's uploaded image, if there is one
    if photo:
        parts.extend(
            [
                types.Part(
                    text=(
                        "USER_PHOTO: observation only, "
                        "not approved technical evidence."
                    )
                ),
                types.Part.from_bytes(
                    data=photo,
                    mime_type="image/png",
                ),
            ]
        )

    # add relevant manual images to the agent's message
    for image in model_images:
        parts.extend(
            [
                types.Part(
                    text=(
                        f"MANUAL_ASSET {image['id']} "
                        f"from evidence {evidence_ids[image['chunk_id']]}"
                    )
                ),
                types.Part.from_bytes(
                    data=image["bytes"],
                    mime_type="image/png",
                ),
            ]
        )

    # prepare a place for the agent's final answer
    metadata(payload_characters=len(parts[0].text), model_image_bytes=(len(photo) if photo else 0)
             + model_image_bytes)
    # Payload building includes context assembly and attaching the selected image bytes.
    trace = current_trace.get()
    if trace:
        event = trace.start('payload_build', payload_started)
        trace.finish(event, payload_started, None, {})
    final_text = ""
    entry.request_stage = "answer generation"
    generation_started = time.monotonic()
    logger.info("Chat stage started: answer generation")

    # run the agent with the question and supporting information
    increment('model_calls')
    usage_responses = 0
    with span('agent', model=cfg.MODEL) as model_measurements:
        async for event in runner.run_async(
            user_id=entry.user_id, session_id=entry.id,
            new_message=types.Content(role='user', parts=parts),
            # One final model call is enough because the response schema and
            # evidence validation happen locally. A second ADK call can turn
            # a temporary Gemini timeout into a 90+ second failed request.
            run_config=RunConfig(max_llm_calls=1),
        ):
            usage = getattr(event, 'usage_metadata', None)
            if usage is not None:
                if usage_responses:
                    increment('model_calls')
                usage_responses += 1
                model_measurements.update(usage_metadata(usage))
            if event.is_final_response() and event.content:
                final_text = ''.join(part.text for part in event.content.parts or []
                                     if part.text and not part.thought)

    logger.info("Chat stage ended: answer generation (%.2fs)",
                time.monotonic() - generation_started)
    entry.request_stage = "answer validation"
    # check that the agent's answer has the expected format
    with span('answer_validation'):
        with span('answer_json_validation'):
            answer = AgentAnswer.model_validate_json(final_text)
        with span('evidence_validation'):
            for block in answer.blocks:
                if any(identifier not in source_ids for identifier in block.evidence_ids):
                    raise ValueError('Model supplied an unknown evidence ID')
                block.evidence_ids = [source_ids[identifier] for identifier in block.evidence_ids]

    # return an unavailable message if the answer is not supported
    if not answer.supported:
        metadata(outcome='unsupported')
        return text_response(cfg.UNAVAILABLE), True

    # A model may mention an image identifier that was not included in the
    # bounded image set. Drop that optional media reference so a valid grounded
    # text answer is still returned instead of failing the whole request.
    supplied_image_ids = {item["id"] for item in media["images"]}
    answer.image_descriptions = [
        item for item in answer.image_descriptions
        if item.asset_id in supplied_image_ids
    ]
    answer.relevant_asset_ids = [
        identifier for identifier in answer.relevant_asset_ids
        if identifier in supplied_image_ids
        or identifier in {item["id"] for item in media["links"]}
    ]
    answer.blocks = [
        block for block in answer.blocks
        if block.type != "image" or block.asset_id in supplied_image_ids
    ]

    # check that the session still exists
    await store.get(entry.id)

    # build the answer for the frontend
    with span('response_build'):
        result = final_blocks(answer, bundle, media, entry, plan.wants_sources)
    metadata(outcome='answered', response_image_count=sum(b['type']=='image' for b in result['blocks']),
             response_link_count=sum(b['type']=='link' for b in result['blocks']))

    # save the topic and guided-answer progress
    with span('session_state_save'):
        await store.update_state(entry, {
            'main_topic': topic, 'guided': guided and not answer.finished,
            'step_number': step, 'finished': answer.finished,
        })

    # add warnings if some images or links could not be loaded
    result["asset_warnings"] = media["warnings"]

    return result, True
