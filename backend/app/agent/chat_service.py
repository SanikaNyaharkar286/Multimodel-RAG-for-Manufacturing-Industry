# this file searches the manuals and uses the agent to answer chat questions


import asyncio
import hashlib
import json
import logging
import re
import time
from uuid import uuid4
from copy import deepcopy
import httpx
from requests.exceptions import ReadTimeout as RequestsReadTimeout
from google.api_core.exceptions import DeadlineExceeded
from google.adk.agents.run_config import RunConfig
from google.adk.runners import Runner
from google.genai import types
from google.genai.errors import APIError, ServerError
from app import config as cfg
from app.agent.agent import root_agent
from app.agent.planner import understand
from app.media.prepare_media import prepare_media
from app.responses.response_builder import text_response, final_blocks
from app.responses.schemas import AgentAnswer, AnswerBlock, ImageDescription, QueryPlan
from app.retrieval.manual_search import list_available_documents, search_manual
from app.sessions.session_store import session_service, store
from .conversation_context import latest_user_photo, recent_context
from .inventory import is_inventory_request, inventory_response
from .repair_workflow import (new_workflow, validate_action, resolve_report,
                              workflow_response, no_further_step)
from app.observability.timing import span, metadata, usage_metadata, increment, current_trace


# connects the agent to the session service
runner = Runner(agent=root_agent, app_name=cfg.APP_NAME, session_service=session_service)
logger = logging.getLogger("uvicorn.error")

# Keep model requests small while retaining the complete retrieval result for
# response rendering and source links.
# Keep the final multimodal request small enough for predictable latency.
MAX_MODEL_EVIDENCE = 8


def _clean_repair_instruction(instruction):
    """Remove model-generated navigation to manual sections from a step."""
    cleaned = re.sub(
        r"\s*(?:Refer to|See|Consult)\s+(?:section|chapter|the manual)\b.*$",
        "",
        instruction.strip(),
        flags=re.IGNORECASE,
    ).strip(" ;:\n")
    return cleaned or instruction.strip()


def _fallback_manual_action(bundle, completed_steps):
    """Extract one explicit corrective action without inventing a repair step."""
    actions = _fallback_manual_actions(bundle, completed_steps)
    return actions[0] if actions else (None, None)


def _fallback_manual_actions(bundle, completed_steps):
    """Extract distinct explicit manual actions in evidence order."""
    seen = {_instruction_key(step.get('instruction', '')) for step in completed_steps}
    actions = []
    for row in bundle.get('chunks', []):
        content = re.sub(r'\s+', ' ', str(row.get('content', '')).strip())
        matches = re.findall(
            r'(?is)(?:CORRECTIVE ACTION|CORRECTION|ACTION TAKEN)\s*:\s*(.+?)(?='
            r'\b(?:SYMPTOM|POSSIBLE CAUSE|CORRECTIVE ACTION|CORRECTION|ACTION TAKEN)\s*:|$)',
            content,
        )
        for match in matches:
            instruction = _clean_repair_instruction(match).strip(' .;')
            if not instruction:
                continue
            key = _instruction_key(instruction)
            if key and key not in seen:
                seen.add(key)
                actions.append((instruction[:1200], row))
    return actions


def _model_fallback_response(media, bundle, entry, guided, question=''):
    """Return a grounded response when the final model is temporarily unavailable."""
    blocks = []
    repair = getattr(entry, 'repair_workflow', None)
    # If intent planning succeeded but final generation timed out, preserve the
    # repair UX for explicit practical requests. Do not expose a raw excerpt
    # dump when the technician asked for a fix or troubleshooting step.
    if not guided and re.search(
            r'\b(?:fix|repair|troubleshoot|what should i (?:check|inspect)|'
            r'tell me how to|how do i|not working|problem|issue|fault)\b',
            question.casefold()):
        guided = True
        if repair is None:
            repair = new_workflow(question, bundle.get('chunks', []))
    if guided and repair:
        actions = _fallback_manual_actions(bundle, repair.get('completed_steps', []))
        instruction, source = actions[0] if actions else (None, None)
        if instruction:
            number = len(repair.get('completed_steps', [])) + 1
            repair['current_step'] = {'step_id': str(uuid4()), 'number': number,
                                      'instruction': instruction}
            repair['status'] = 'awaiting_done'
            blocks.append({'type': 'text', 'content': f'### Step {number}\n{instruction}'})
            # Keep later actions private so Done can advance without another
            # slow Gemini request or a fresh retrieval pass.
            remaining = [
                {'instruction': action, 'chunk_id': row.get('chunk_id', '')}
                for action, row in actions[1:]
            ]
            metadata(fallback_step=True, fallback_evidence_id=source.get('source_id', ''))
        else:
            result = no_further_step(repair)
            entry.repair_workflow = repair
            return result
    else:
        message = "The answer service is temporarily unavailable. Relevant manual excerpts are shown below."
        excerpts = []
        for row in bundle.get("chunks", [])[:3]:
            content = str(row.get("content", "")).strip()
            if content:
                excerpts.append(content[:1800])
        if excerpts:
            message += "\n\n### Manual excerpts\n\n" + "\n\n---\n\n".join(excerpts)
        blocks.append({"type": "text", "content": message})
    shown_chunk = source.get('chunk_id') if guided and repair and source else None
    image_assets = [asset for asset in media.get("images", [])
                    if not shown_chunk or asset.get('chunk_id') == shown_chunk]
    seen_image_digests = set()
    for asset in image_assets:
        digest = hashlib.sha256(asset.get('bytes', asset['id'].encode())).digest()
        if digest in seen_image_digests:
            continue
        seen_image_digests.add(digest)
        asset_id = store.put_image(entry, asset["bytes"])
        blocks.append({
            "type": "image",
            "src": f"/images/{asset_id}",
            "caption": "Relevant original diagram from the approved manual.",
            "description": "Relevant manual image retained for this request.",
        })
    result = {"blocks": blocks, "degraded": True}
    if guided and repair:
        entry.repair_workflow = repair
        cache = getattr(entry, 'repair_cache', None) or {}
        entry.repair_cache = {**cache, 'bundle': bundle, 'media': media,
                              'workflow_id': repair['workflow_id'],
                              'plan_complete': bool(remaining), 'remaining_answers': remaining,
                              'wants_sources': cache.get('wants_sources', False)}
        workflow_response(result, repair)
    return result


def _explicit_follow_up_request(question):
    """Return true only for wording that clearly asks to continue a prior task."""
    text = re.sub(r"[^a-z0-9 ]+", " ", question.lower())
    return bool(re.search(
        r"\b(next step|next|continue|what should i check now|what do i check next|why that|why this check|then what)\b",
        text,
    ))


def _instruction_key(instruction):
    return re.sub(r'[^\w]+', ' ', _clean_repair_instruction(instruction).casefold()).strip()


def _apply_repair_step(answer, repair):
    """Render one validated action, keeping future actions private."""
    instruction = _clean_repair_instruction(answer.repair_step.instruction)
    if any(_instruction_key(item['instruction']) == _instruction_key(instruction)
           for item in repair['completed_steps']):
        raise ValueError('Model repeated an already completed repair step')
    number = len(repair['completed_steps']) + 1
    repair['current_step'] = {'step_id': str(uuid4()), 'number': number,
                              'instruction': instruction}
    repair['status'] = 'awaiting_done'
    answer.blocks = [AnswerBlock(type='text', content=f'### Step {number}\n{instruction}',
                                 evidence_ids=answer.repair_step.evidence_ids)] + [
        block for block in answer.blocks if block.type in ('image', 'link')]


def _include_all_prepared_images(answer, media):
    """Keep every prepared manual figure visible for an initial visual repair request."""
    media_by_id = {asset['id']: asset for asset in media.get('images', [])}
    selected = []
    seen_digests = set()
    for asset_id in answer.relevant_asset_ids:
        asset = media_by_id.get(asset_id)
        if not asset:
            selected.append(asset_id)
            continue
        digest = hashlib.sha256(asset.get('bytes', asset['id'].encode())).digest()
        if digest not in seen_digests:
            selected.append(asset_id)
            seen_digests.add(digest)
    described = {item.asset_id for item in answer.image_descriptions}
    for asset in media.get('images', []):
        digest = hashlib.sha256(asset.get('bytes', asset['id'].encode())).digest()
        if digest in seen_digests:
            continue
        seen_digests.add(digest)
        asset_id = asset['id']
        if asset_id not in selected:
            selected.append(asset_id)
        if asset_id not in described:
            answer.image_descriptions.append(ImageDescription(
                asset_id=asset_id,
                description='Relevant original diagram from the approved manual. '
                            'Use the numbered labels to identify the component referenced by this repair step.',
            ))
    answer.relevant_asset_ids = selected


async def _advance_cached_plan(entry, repair, cached):
    """Done never needs a model, search or media download for a complete plan."""
    with span('repair_plan_advance'):
        remaining = cached['remaining_answers']
        if remaining:
            planned = remaining[0]
            if isinstance(planned, dict):
                number = len(repair.get('completed_steps', [])) + 1
                repair['current_step'] = {'step_id': str(uuid4()), 'number': number,
                                          'instruction': planned['instruction']}
                repair['status'] = 'awaiting_done'
                result = {'blocks': [{'type': 'text',
                                      'content': f"### Step {number}\n{planned['instruction']}"}],
                          'degraded': True}
                for asset in cached['media'].get('images', []):
                    if asset.get('chunk_id') != planned.get('chunk_id'):
                        continue
                    asset_id = store.put_image(entry, asset['bytes'])
                    result['blocks'].append({
                        'type': 'image', 'src': f'/images/{asset_id}',
                        'caption': 'Relevant original diagram from the approved manual.',
                        'description': 'Relevant manual image for this repair step.',
                    })
                    break
            else:
                answer = planned.model_copy(deep=True)
                _apply_repair_step(answer, repair)
                result = final_blocks(answer, cached['bundle'], cached['media'], entry,
                                      cached['wants_sources'])
            workflow_response(result, repair)
        else:
            result = no_further_step(repair)
        # Commit only after rendering and persistence succeed.
        await store.update_state(entry, {'main_topic': repair['issue'], 'guided': True,
                                        'step_number': len(repair['completed_steps']) + bool(remaining),
                                        'finished': False})
        if remaining:
            remaining.pop(0)
        entry.repair_workflow = repair
        result['asset_warnings'] = cached['media']['warnings']
        increment('cache_hits')
        metadata(outcome='answered' if remaining or repair['current_step'] else 'completed',
                 repair_plan_cache_hit=True)
        return result, False


# plans the search and gets an answer for the user's question
async def answer_question(entry, question, photo, repair_action=None):
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
    repair = deepcopy(getattr(entry, 'repair_workflow', None))
    if repair and repair['status'] != 'resolved':
        # Keep a paused repair separate from the most recent informational topic.
        state = {**state, 'active_repair': {'issue': repair['issue'],
                 'current_step': repair.get('current_step')}}
    if repair_action:
        repair = validate_action(repair, repair_action)
        if repair_action.action == 'report_issue':
            # The observed result can invalidate every precomputed next step.
            entry.repair_cache = None
        if repair_action.action == 'resolve':
            result = resolve_report(repair, repair_action)
            await store.update_state(entry, {'guided': False, 'finished': True})
            entry.repair_workflow = repair
            entry.repair_cache = None
            return result, False
        cached = getattr(entry, 'repair_cache', None)
        if (repair_action.action == 'done' and cached and cached.get('workflow_id') == repair['workflow_id']
                and cached.get('plan_complete')):
            return await _advance_cached_plan(entry, repair, cached)

    async def answer_inventory():
        documents = await stage("manual inventory lookup", list_available_documents)
        metadata(outcome='inventory')
        return text_response(inventory_response(documents)), False

    # Common catalog requests need a full grouped query, not ranked manual text.
    if repair_action is None and photo is None and is_inventory_request(question):
        return await answer_inventory()

    # understand what the user is asking
    previous_photo = (
        getattr(entry, "uploaded_photo", None) or latest_user_photo(entry.history)
    ) if photo is None else None
    report_context = ''
    if repair_action and repair_action.action == 'report_issue':
        report_context = f" Technician report: {repair_action.issue_report.strip()}"
    plan = QueryPlan(
        query=f"{repair['issue']}. Next manual-supported action after: " + '; '.join(
            item['instruction'] for item in repair['completed_steps']) + report_context,
        topic=repair['issue'], follow_up=True, guided=True, repair=True, advance=True,
        wants_visual=False, wants_video=False, wants_sources=False, clarify='',
        out_of_scope=False, model_mismatch=False,
    ) if repair_action else await stage(
        "image and question analysis" if photo or previous_photo else "question analysis",
        understand,
        question,
        state,
        entry.history,
        photo,
        previous_photo,
    )
    # A typed next-step request cannot confirm physical work. Only Done advances.
    plan.advance = repair_action is not None

    # A new upload normally starts a new subject. Do not let the planner carry
    # the previous topic into it unless the wording explicitly asks to continue.
    if photo is not None and plan.follow_up and not _explicit_follow_up_request(question):
        plan.follow_up = False
        plan.use_previous_photo = False
        plan.advance = False
        plan.guided = False

    if plan.repair and not repair_action and not plan.follow_up:
        repair = None
        entry.repair_workflow = None
        entry.repair_cache = None

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

    # Classify the current request, rather than inheriting guided mode from history.
    guided = bool(repair_action or plan.repair)
    if guided and repair and repair['status'] != 'resolved':
        topic = repair['issue']
    elif not guided and repair:
        topic = plan.topic or plan.query
    elif plan.follow_up and not using_previous_photo:
        topic = state.get("main_topic", "")
    else:
        topic = plan.topic

    # choose a topic or search query
    topic = topic or plan.topic or plan.query

    if repair and repair['status'] == 'resolved':
        repair = None
    request_step = guided and (repair is None or repair_action is not None
                              or repair['current_step'] is None)

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
    cached_repair = (getattr(entry, "repair_cache", None)
                     if repair_action and repair_action.action == 'done' else None)
    if cached_repair:
        bundle = cached_repair["bundle"]
        metadata(cache_hits=1)
        logger.info("Reusing cached repair evidence for session %s", entry.id)
    else:
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
        if repair_action:
            result = no_further_step(repair)
            entry.repair_workflow = repair
            return result, False
        return text_response(cfg.UNAVAILABLE), False

    if guided and repair is None:
        repair = new_workflow(topic, bundle['chunks'])
    if guided:
        step = len(repair['completed_steps']) + 1

    # prepare related images and links
    logger.info("Retrieved %d chunks, %d image references and %d links",
                len(bundle["chunks"]), len(bundle["images"]), len(bundle["links"]))
    if cached_repair:
        media = cached_repair["media"]
    else:
        media = await stage(
            "manual image and link loading",
            prepare_media,
            bundle,
            plan.wants_visual,
            plan.wants_video,
            guided or plan.wants_visual,
        )

    logger.info("Prepared %d images and %d links; %d asset warnings",
                len(media["images"]), len(media["links"]), len(media["warnings"]))
    metadata(chunk_count=len(bundle['chunks']), response_image_count=len(media['images']),
             response_link_count=len(media['links']), asset_warning_count=len(media['warnings']))
    if guided and repair_action is None and not cached_repair:
        entry.repair_cache = {'bundle': bundle, 'media': media}

    # collect the current topic and answer settings
    payload_started = time.perf_counter()
    workflow = {
        "MAIN_TOPIC": topic,
        "GUIDED": guided,
        "STEP_NUMBER": step,
        "ADVANCE": plan.advance,
        "FOLLOW_UP": plan.follow_up,
        "CURRENT_STEP_ALREADY_GIVEN": bool(current_step),
        "REQUEST_STEP": request_step,
        "CURRENT_STEP": repair.get('current_step') if guided else None,
        "COMPLETED_STEPS": repair['completed_steps'] if guided else [],
        "REPORTED_ISSUES": repair.get('reported_issues', []) if guided else [],
        "wants_sources": plan.wants_sources,
        "wants_visual": plan.wants_visual,
        "wants_video": plan.wants_video,
    }

    # prepare recent messages to give the agent context
    recent = recent_context(entry.history, text_limit=1800)

    # Guided repair needs the complete retrieved manual context so Gemini can
    # plan every supported action and associate every required figure. The UI
    # still releases only one action at a time; future actions stay private in
    # the repair cache. Informational requests keep the smaller fast payload.
    model_chunks = list(
        bundle["chunks"] if guided else bundle["chunks"][:MAX_MODEL_EVIDENCE]
    )
    # Every supplied figure/link needs its evidence identity, even when its
    # chunk is below the normal text ranking cutoff. Keep required media.
    selected_chunk_ids = {row['chunk_id'] for row in model_chunks}
    media_chunk_ids = {item['chunk_id'] for item in media['images'] + media['links']}
    for row in bundle['chunks']:
        if row['chunk_id'] in media_chunk_ids and row['chunk_id'] not in selected_chunk_ids:
            model_chunks.append(row)
            selected_chunk_ids.add(row['chunk_id'])

    # Retrieval rows contain geometry, relationships, table expansions and
    # other renderer metadata. The answer model only needs concise technical
    # evidence and source identity; sending the rest wastes tokens and time.
    compact_chunks = []
    for row in model_chunks:
        compact = {
            "chunk_id": row["chunk_id"],
            "content": str(row.get("content", ""))[:5000],
            "document_name": row.get("document_name", ""),
            "page_number": row.get("page_number"),
            "source_id": row.get("source_id", ""),
            "content_type": row.get("content_type", ""),
        }
        compact_chunks.append(compact)

    # give the evidence short ids for this request
    evidence_ids = {
        row["chunk_id"]: f"evidence-{index + 1}"
        for index, row in enumerate(model_chunks)
    }
    source_ids = {value: key for key, value in evidence_ids.items()}

    # Media preparation normalizes/caches figures. Do not silently discard
    # required images because the question needs six (or more) of them.
    model_images = [image for image in media['images'] if image['chunk_id'] in evidence_ids]
    model_image_bytes = sum(len(image['bytes']) for image in model_images)

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
            for row in compact_chunks
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
    try:
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
    except (APIError, ServerError, TimeoutError, asyncio.TimeoutError,
            RequestsReadTimeout, httpx.ReadTimeout, DeadlineExceeded) as error:
        # Do not issue a second Gemini request here. ADK/provider cancellation
        # can outlive asyncio.wait_for and turn one failure into a 60–120s
        # request. Approved evidence is already available for a safe response.
        metadata(outcome='model_fallback', error_type=type(error).__name__)
        metadata(provider_error_code=getattr(error, 'code', None),
                 provider_error_status=getattr(error, 'status', None))
        logger.warning("Model request failed; returning evidence fallback error_type=%s",
                       type(error).__name__)
        return _model_fallback_response(media, bundle, entry, guided, question), False

    logger.info("Chat stage ended: answer generation (%.2fs)",
                time.monotonic() - generation_started)
    entry.request_stage = "answer validation"
    # check that the agent's answer has the expected format
    with span('answer_validation'):
        with span('answer_json_validation'):
            answer = AgentAnswer.model_validate_json(final_text)
        with span('evidence_validation'):
            for block in (answer.blocks + ([answer.repair_step] if answer.repair_step else [])
                          + answer.remaining_repair_steps):
                if any(identifier not in source_ids for identifier in block.evidence_ids):
                    raise ValueError('Model supplied an unknown evidence ID')
                block.evidence_ids = [source_ids[identifier] for identifier in block.evidence_ids]

    # return an unavailable message if the answer is not supported
    if not answer.supported:
        metadata(outcome='unsupported')
        if request_step and repair:
            result = no_further_step(repair)
            entry.repair_workflow = repair
            return result, True
        return text_response(cfg.UNAVAILABLE), True

    if (not request_step and (answer.repair_step is not None or answer.remaining_repair_steps)):
        raise ValueError('A new repair step requires the current step Done confirmation')

    remaining_answers = []
    if request_step:
        if answer.repair_step is None:
            result = no_further_step(repair)
            entry.repair_workflow = repair
            return result, True
        seen = {_instruction_key(item['instruction']) for item in repair['completed_steps']}
        allowed_assets = {item['id'] for item in media['images'] + media['links']}
        for planned in [answer.repair_step] + answer.remaining_repair_steps:
            planned.instruction = _clean_repair_instruction(planned.instruction)
            key = _instruction_key(planned.instruction)
            if key in seen:
                raise ValueError('Model repeated a repair action in the procedure')
            seen.add(key)
            if set(planned.asset_ids) - allowed_assets:
                raise ValueError('Model supplied an unknown repair asset ID')
        # Keep validated future actions in the private session cache, never in
        # the returned workflow/history until the technician presses Done.
        for planned in answer.remaining_repair_steps:
            assets = set(planned.asset_ids)
            future = answer.model_copy(deep=True)
            future.repair_step = planned.model_copy(deep=True)
            future.remaining_repair_steps = []
            future.blocks = [block for block in answer.blocks
                             if block.type in ('image', 'link') and block.asset_id in assets]
            future.relevant_asset_ids = list(planned.asset_ids)
            remaining_answers.append(future)
        answer.relevant_asset_ids = list(dict.fromkeys(
            answer.relevant_asset_ids + answer.repair_step.asset_ids))
        _apply_repair_step(answer, repair)

    # Keep only media that was actually prepared for this request. This also
    # makes an optional model reference harmless when an asset failed to load.
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

    # Keep the complete prepared figure set visible for the initial visual
    # repair request. This does not expose future repair actions; it only
    # preserves all supporting images the retrieval stage already selected.
    if guided and repair_action is None and media['images']:
        _include_all_prepared_images(answer, media)

    # check that the session still exists
    await store.get(entry.id)

    # build the answer for the frontend
    with span('response_build'):
        result = final_blocks(answer, bundle, media, entry, plan.wants_sources)
        # Validate future rendering now. Done must not discover a missing image
        # description, fabricated reference or duplicate instruction later.
        for future in remaining_answers:
            preview = future.model_copy(deep=True)
            preview_repair = deepcopy(repair)
            _apply_repair_step(preview, preview_repair)
            final_blocks(preview, bundle, media, entry, plan.wants_sources)
    if guided:
        workflow_response(result, repair)
    metadata(outcome='answered', response_image_count=sum(b['type']=='image' for b in result['blocks']),
             response_link_count=sum(b['type']=='link' for b in result['blocks']))

    # save the topic and guided-answer progress
    with span('session_state_save'):
        await store.update_state(entry, {
            'main_topic': topic, 'guided': guided,
            'step_number': step, 'finished': False if guided else answer.finished,
        })
    if guided:
        entry.repair_workflow = repair
        if request_step:
            entry.repair_cache = {'bundle': bundle, 'media': media,
                                 'workflow_id': repair['workflow_id'],
                                 'plan_complete': bool(answer.repair_plan_complete or remaining_answers),
                                 'remaining_answers': remaining_answers,
                                 'wants_sources': plan.wants_sources}
            metadata(repair_plan_step_count=1 + len(remaining_answers))

    # add warnings if some images or links could not be loaded
    result["asset_warnings"] = media["warnings"]

    return result, True
