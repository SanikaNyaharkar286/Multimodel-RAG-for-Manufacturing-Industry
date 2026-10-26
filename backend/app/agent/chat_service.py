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
from app.agent.agent import lite_agent, root_agent
from app.agent.safety import start_safety
from app.agent.planner import understand
from app.media.prepare_media import prepare_media
from app.responses.response_builder import text_response, final_blocks
from app.responses.schemas import AgentAnswer, AnswerBlock, ImageDescription, QueryPlan
from app.retrieval.manual_search import list_available_documents, search_manual
from app.sessions.session_store import session_service, store
from .conversation_context import latest_user_photo, recent_context
from .request_context import case_questions, clearly_new_topic, repeated_case_question
from .inventory import is_inventory_request, inventory_response
from .repair_workflow import (new_workflow, validate_action, resolve_report,
                              workflow_response, no_further_step)
from app.observability.timing import span, metadata, usage_metadata, increment, current_trace


# connects the agent to the session service
runner = Runner(agent=root_agent, app_name=cfg.APP_NAME, session_service=session_service)
lite_runner = Runner(agent=lite_agent, app_name=cfg.APP_NAME, session_service=session_service)
logger = logging.getLogger("uvicorn.error")



def _clean_repair_instruction(instruction):
    """Remove model-generated navigation to manual sections from a step."""
    original = instruction.strip()
    cleaned = re.sub(
        r"\s*(?:Refer to|See|Consult)\s+(?:the\s+)?(?:section|chapter|manual)\b.*$",
        "",
        original,
        flags=re.IGNORECASE,
    ).strip(" ;:\n")
    if cleaned:
        return cleaned
    # A model should use the expanded section evidence, but old cached plans
    # can still contain a navigation-only instruction. Turn its stated purpose
    # into an action instead of exposing "refer to section" in the UI/PDF.
    purpose = re.search(
        r'(?i)\bfor\s+((?:adjusting|checking|setting|installing|replacing|cleaning|inspecting)\b[^.;]*)',
        original,
    )
    if purpose:
        action = purpose.group(1).strip()
        replacements = {
            'adjusting': 'Adjust', 'checking': 'Check', 'setting': 'Set',
            'installing': 'Install', 'replacing': 'Replace',
            'cleaning': 'Clean', 'inspecting': 'Inspect',
        }
        first, _, rest = action.partition(' ')
        return replacements[first.casefold()] + (f' {rest}' if rest else '')
    target = re.sub(
        r'(?i)^.*?\b(?:section|chapter)\s+', '', original).strip(' .;:')
    target = re.split(r'(?i)\s+of\s+the\s+', target, maxsplit=1)[0]
    return f'Follow the approved {target} procedure shown in this step.'


def _step_asset_ids(step, bundle, media):
    """Associate every prepared figure/link on a step's evidence pages."""
    evidence = {row['chunk_id']: row for row in bundle.get('chunks', [])}
    pages = {
        (str(evidence[identifier].get('machine_name', '')).casefold(),
         str(evidence[identifier].get('document_name', '')).casefold().removesuffix('.pdf'),
         evidence[identifier].get('page_number'))
        for identifier in step.evidence_ids if identifier in evidence
    }
    related = []
    for item in media.get('images', []) + media.get('links', []):
        row = evidence.get(item.get('chunk_id'), {})
        page = (
            str(item.get('machine_name', row.get('machine_name', ''))).casefold(),
            str(item.get('document', item.get('document_name', row.get('document_name', '')))).casefold().removesuffix('.pdf'),
            item.get('page', item.get('page_number', row.get('page_number'))),
        )
        if item.get('chunk_id') in step.evidence_ids or page in pages:
            related.append(item['id'])
    return list(dict.fromkeys(step.asset_ids + related))


def _ensure_step_image_descriptions(answer, asset_ids, bundle, media):
    """Give automatically associated figures grounded source descriptions."""
    described = {item.asset_id for item in answer.image_descriptions}
    evidence = {row['chunk_id']: row for row in bundle.get('chunks', [])}
    images = {item['id']: item for item in media.get('images', [])}
    for identifier in asset_ids:
        if identifier in described or identifier not in images:
            continue
        asset = images[identifier]
        row = evidence.get(asset.get('chunk_id'), {})
        document = asset.get('document', row.get('document_name', 'the approved manual'))
        page = asset.get('page', row.get('page_number'))
        context = re.sub(r'\s+', ' ', str(row.get('content', '')).strip())[:500]
        description = f'Original manual figure from {document}'
        if page is not None:
            description += f', PDF page {page}'
        description += ', associated with the current repair step.'
        if context:
            description += f' Manual context: {context}'
        answer.image_descriptions.append(
            ImageDescription(asset_id=identifier, description=description))
        described.add(identifier)


def _assign_all_step_images(steps, bundle, media):
    """Distribute every prepared manual image across the guided steps."""
    if not steps:
        return
    evidence = {row['chunk_id']: row for row in bundle.get('chunks', [])}
    step_pages = []
    for step in steps:
        step.asset_ids = _step_asset_ids(step, bundle, media)
        pages = []
        for identifier in step.evidence_ids:
            row = evidence.get(identifier)
            if row:
                pages.append((
                    str(row.get('machine_name', '')).casefold(),
                    str(row.get('document_name', '')).casefold().removesuffix('.pdf'),
                    row.get('page_number'),
                ))
        step_pages.append(pages)

    assigned = {identifier for step in steps for identifier in step.asset_ids}
    for image in media.get('images', []):
        if image['id'] in assigned:
            continue
        row = evidence.get(image.get('chunk_id'), {})
        image_page = (
            str(image.get('machine_name', row.get('machine_name', ''))).casefold(),
            str(image.get('document', image.get('document_name', row.get('document_name', '')))).casefold().removesuffix('.pdf'),
            image.get('page', image.get('page_number', row.get('page_number'))),
        )

        def distance(index):
            candidates = step_pages[index]
            same_document = [page for page in candidates
                             if page[:2] == image_page[:2]]
            if same_document and isinstance(image_page[2], int):
                distances = [abs(page[2] - image_page[2])
                             for page in same_document if isinstance(page[2], int)]
                if distances:
                    return min(distances)
            return 100000 + index

        target = min(range(len(steps)), key=distance)
        steps[target].asset_ids.append(image['id'])
        assigned.add(image['id'])


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


def _fallback_step_assets(actions, bundle, media):
    """Assign every prepared manual image to one fallback action."""
    if not actions:
        return []
    evidence = {row['chunk_id']: row for row in bundle.get('chunks', [])}
    pages = [(
        str(row.get('machine_name', '')).casefold(),
        str(row.get('document_name', '')).casefold().removesuffix('.pdf'),
        row.get('page_number'),
    ) for _, row in actions]
    assignments = [[] for _ in actions]
    for asset in media.get('images', []):
        row = evidence.get(asset.get('chunk_id'), {})
        asset_page = (
            str(asset.get('machine_name', row.get('machine_name', ''))).casefold(),
            str(asset.get('document', asset.get('document_name',
                                                row.get('document_name', '')))).casefold().removesuffix('.pdf'),
            asset.get('page', asset.get('page_number', row.get('page_number'))),
        )

        def distance(index):
            page = pages[index]
            if page[:2] != asset_page[:2]:
                return 100000 + index
            if isinstance(page[2], int) and isinstance(asset_page[2], int):
                return abs(page[2] - asset_page[2])
            return 50000 + index

        assignments[min(range(len(actions)), key=distance)].append(asset['id'])
    return assignments


def _model_fallback_response(media, bundle, entry, guided, question='', workflow=None):
    """Return a grounded response when the final model is temporarily unavailable."""
    blocks = []
    repair = workflow or getattr(entry, 'repair_workflow', None)
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
        action_assets = _fallback_step_assets(actions, bundle, media)
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
                {'instruction': action, 'chunk_id': row.get('chunk_id', ''),
                 'asset_ids': action_assets[index]}
                for index, (action, row) in enumerate(actions[1:], start=1)
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
    visible_fallback_ids = set(action_assets[0]) if guided and repair and source else set()
    image_assets = [asset for asset in media.get("images", [])
                    if not visible_fallback_ids or asset.get('id') in visible_fallback_ids]
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
            "caption": f"Original diagram — {asset.get('document', 'approved manual')}, PDF page {asset.get('page', 'unknown')}.",
            "description": f"Original manual figure supporting Step {number}: {instruction}" if guided and repair else "Relevant manual image retained for this request.",
            "source": {"document_name": asset.get('document'),
                       "page_number": asset.get('page')},
        })
    result = {"blocks": blocks, "degraded": True, "documentable": False}
    if guided and repair:
        entry.repair_workflow = repair
        cache = getattr(entry, 'repair_cache', None) or {}
        entry.repair_cache = {**cache, 'bundle': bundle, 'media': media,
                              'workflow_id': repair['workflow_id'],
                              'plan_complete': True, 'remaining_answers': remaining,
                              'wants_sources': cache.get('wants_sources', False),
                              'images_assigned_to_steps': True}
        workflow_response(result, repair)
    return result


def _explicit_follow_up_request(question):
    """Return true only for wording that clearly asks to continue a prior task."""
    text = re.sub(r"[^a-z0-9 ]+", " ", question.lower())
    return bool(re.search(
        r"\b(next step|next|continue|what should i check now|what do i check next|why that|why this check|then what)\b",
        text,
    ))


def _smalltalk_reply(question):
    """Answer common social and assistant-capability questions outside manual RAG."""
    raw = question.casefold().replace("’", "'")
    for contraction, expanded in (("what's", "what is"), ("who's", "who is"),
                                  ("how's", "how is"), ("i'm", "i am")):
        raw = re.sub(rf"\b{re.escape(contraction)}\b", expanded, raw)
    raw = re.sub(r"\br u\b", "are you", raw)
    raw = re.sub(r"\bu\b", "you", raw)
    text = re.sub(r"[^a-z0-9 ]+", " ", raw).strip()
    text = re.sub(r"\s+", " ", text)
    greeting = r"(?:hi|hello|hey|good morning|good afternoon|good evening)(?: there)?"
    social_question = (r"(?:how are you(?: doing)?|how is it going|how are things|"
                       r"what is up|how have you been)")
    if re.fullmatch(rf"(?:{greeting})(?: {social_question})?", text):
        return "Hi! I'm doing well, thanks. I'm ready to help with equipment questions or troubleshooting."
    if re.fullmatch(rf"{social_question}(?: today)?", text):
        return "I'm doing well, thanks! I'm ready to help with equipment questions or troubleshooting."
    if re.fullmatch(r"(?:thanks|thank you)(?: so much| very much| again)?", text):
        return "You're welcome. I'm here if you need help with a machine, manual, or maintenance task."
    if re.fullmatch(
        r"(?:who are you|what are you|what is your name|who made you|who created you|"
        r"what do you do|what is your purpose|what is your role|what is your job|"
        r"what are you here for|what are you capable of|introduce yourself)",
        text,
    ):
        return (
            "I'm the Manufacturing Equipment Copilot, an assistant for technicians working "
            "with manufacturing equipment. I can help find manual-based answers, explain "
            "relevant diagrams, guide supported troubleshooting one step at a time, and "
            "document maintenance results."
        )
    if re.fullmatch(r"(?:can|could) you help me(?: please)?", text):
        return "Yes. Tell me the machine model and what you observed, or upload a photo, and I'll help find the relevant manual guidance."
    if re.fullmatch(r"(?:how can you help|what can you help me with|what are your capabilities)", text):
        return (
            "I can search the approved equipment manuals, explain relevant components and "
            "diagrams, guide supported repairs one step at a time, and help document the "
            "maintenance result. Tell me the machine model and what you need."
        )
    if (re.search(r"\b(?:what|tell|explain|describe|how)\b", text)
            and re.search(r"\b(?:agent|copilot|assistant)\b", text)
            and re.search(r"\b(?:use|purpose|do|help|work|capabilit|function)\w*\b", text)):
        return (
            "I help technicians find answers in the approved machine manuals, locate relevant "
            "diagrams, and work through supported maintenance steps one at a time. You can "
            "describe a fault, ask about a machine or component, or upload a photo. I keep "
            "repair steps grounded in the available manuals and can help document the outcome."
        )
    return None


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
                              'instruction': instruction,
                              **answer.repair_step.model_dump(exclude={'instruction', 'evidence_ids', 'asset_ids'})}
    repair['current_step']['expected_result'] = answer.repair_step.expected_result or 'A specific expected result is not recorded in the retrieved evidence. Record what you observe.'
    repair['status'] = 'awaiting_done'
    answer.blocks = [AnswerBlock(type='text', content=f'### Step {number}\n{instruction}',
                                 evidence_ids=answer.repair_step.evidence_ids)] + [
        block for block in answer.blocks if block.type in ('image', 'link')]


def validate_branches(steps):
    keys = [step.step_key for step in steps]
    if any(keys) and len([key for key in keys if key]) != len(set(key for key in keys if key)):
        raise ValueError('Duplicate plan step key')
    for index, step in enumerate(steps):
        for branch in step.branches:
            if branch.next_step_key != 'END' and branch.next_step_key not in keys[index + 1:]:
                raise ValueError('Branch must target a later prepared step or END')


async def attach_safety(entry, repair, bundle):
    if repair and getattr(entry, 'safety_task', None):
        repair['safety'] = await entry.safety_task
        repair['safety']['sources'] = [
            {'document_name': row['document_name'], 'page_number': row['page_number']}
            for row in bundle['chunks'] if re.search(r'safety|lockout|isolate|warning', str(row.get('content', '')), re.I)]


async def _advance_cached_plan(entry, repair, cached):
    """Done never needs a model, search or media download for a complete plan."""
    with span('repair_plan_advance'):
        remaining = list(cached['remaining_answers'])
        previous = repair['completed_steps'][-1] if repair['completed_steps'] else {}
        branches = previous.get('branches', [])
        if branches:
            target = next(branch['next_step_key'] for branch in branches
                          if branch['label'] == previous.get('observation'))
            if target == 'END':
                remaining = []
            else:
                index = next((i for i, answer in enumerate(remaining)
                              if not isinstance(answer, dict) and answer.repair_step.step_key == target), None)
                if index is None:
                    raise ValueError('Prepared branch target missing')
                remaining = remaining[index:]
            repair['conditional'] = any(not isinstance(answer, dict) and answer.repair_step.branches for answer in remaining)
            repair['total_steps'] = None if repair['conditional'] else len(repair['completed_steps']) + len(remaining)
        if not cached.get('images_assigned_to_steps'):
            cached_steps = [item.repair_step for item in remaining
                            if not isinstance(item, dict) and item.repair_step]
            _assign_all_step_images(cached_steps, cached['bundle'], cached['media'])
            cached['images_assigned_to_steps'] = True
        if remaining:
            planned = remaining[0]
            if isinstance(planned, dict):
                number = len(repair.get('completed_steps', [])) + 1
                instruction = _clean_repair_instruction(planned['instruction'])
                repair['current_step'] = {'step_id': str(uuid4()), 'number': number,
                                          'instruction': instruction}
                repair['status'] = 'awaiting_done'
                result = {'blocks': [{'type': 'text',
                                      'content': f"### Step {number}\n{instruction}"}],
                          'degraded': True}
                source = next((row for row in cached['bundle'].get('chunks', [])
                               if row.get('chunk_id') == planned.get('chunk_id')), {})
                source_page = (
                    str(source.get('machine_name', '')).casefold(),
                    str(source.get('document_name', '')).casefold().removesuffix('.pdf'),
                    source.get('page_number'),
                )
                shown = set()
                for asset in cached['media'].get('images', []):
                    asset_page = (
                        str(asset.get('machine_name', '')).casefold(),
                        str(asset.get('document', asset.get('document_name', ''))).casefold().removesuffix('.pdf'),
                        asset.get('page', asset.get('page_number')),
                    )
                    selected_ids = set(planned.get('asset_ids', []))
                    if (asset.get('id') not in selected_ids
                            and asset.get('chunk_id') != planned.get('chunk_id')
                            and not (source and asset_page == source_page)):
                        continue
                    digest = hashlib.sha256(asset['bytes']).digest()
                    if digest in shown:
                        continue
                    shown.add(digest)
                    asset_id = store.put_image(entry, asset['bytes'])
                    document = asset.get('document', source.get('document_name', 'the approved manual'))
                    page = asset.get('page', source.get('page_number'))
                    result['blocks'].append({
                        'type': 'image', 'src': f'/images/{asset_id}',
                        'caption': f'Original diagram — {document}, PDF page {page}.',
                        'description': f'Original manual figure supporting Step {number}: {instruction}',
                        'source': {'document_name': document, 'page_number': page},
                    })
                result['sources'] = ([{'document_name': source.get('document_name'),
                                       'page_number': source.get('page_number')}]
                                     if source else [])
            else:
                answer = planned.model_copy(deep=True)
                answer.repair_step.instruction = _clean_repair_instruction(
                    answer.repair_step.instruction)
                answer.relevant_asset_ids = list(answer.repair_step.asset_ids)
                _ensure_step_image_descriptions(
                    answer, answer.repair_step.asset_ids,
                    cached['bundle'], cached['media'])
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
        cached['remaining_answers'] = remaining
        entry.repair_workflow = repair
        result['asset_warnings'] = cached['media']['warnings']
        increment('cache_hits')
        metadata(outcome='answered' if remaining or repair['current_step'] else 'completed',
                 repair_plan_cache_hit=True)
        return result, False


# plans the search and gets an answer for the user's question
async def answer_question(entry, question, photo, repair_action=None, start_new_case=False):
    async def stage(name, function, *args):
        entry.request_stage = name
        if getattr(entry, 'current_progress', None) is not None:
            entry.current_progress['stage'] = {
                'manual evidence retrieval': 'Searching the manuals',
                'manual image and link loading': 'Loading relevant manual figures',
                'question analysis': 'Classifying your request',
                'image and question analysis': 'Analysing the uploaded image',
            }.get(name, name)
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
    if start_new_case and repair:
        entry.cases[repair['workflow_id']] = deepcopy(repair)
        repair = None
        entry.repair_workflow = None
        entry.repair_cache = None
        state = {}
    if repair and repair['status'] not in ('resolved', 'unresolved'):
        # Keep a paused repair separate from the most recent informational topic.
        state = {**state, 'active_repair': {'issue': repair['issue'],
                 'machine': repair.get('machine', ''),
                 'current_step': repair.get('current_step'),
                 'original_questions': case_questions(entry.history, repair['workflow_id'])}}
    if repair_action:
        repair = validate_action(repair, repair_action)
        if repair_action.action == 'report_issue':
            # The observed result can invalidate every precomputed next step.
            entry.repair_cache = None
        if repair_action.action in ('resolve', 'unresolved'):
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

    # Greetings and questions about the Copilot itself are not manual lookups.
    # Answer them directly instead of incorrectly claiming the PDFs lack the
    # information or creating a troubleshooting workflow.
    if repair_action is None and photo is None:
        smalltalk = _smalltalk_reply(question)
        if smalltalk:
            metadata(outcome='smalltalk')
            response = text_response(smalltalk, documentable=False)
            response['request_type'] = 'smalltalk'
            return response, False

    # understand what the user is asking
    previous_photo = (
        getattr(entry, "uploaded_photo", None) or latest_user_photo(entry.history)
    ) if photo is None and not start_new_case else None
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
        [] if start_new_case else entry.history,
        photo,
        previous_photo,
    )
    # A typed next-step request cannot confirm physical work. Only Done advances.
    plan.advance = repair_action is not None
    if (repair and repair['status'] not in ('resolved', 'unresolved') and not repair_action
            and repeated_case_question(question, repair['issue'],
                                       case_questions(entry.history, repair['workflow_id']))):
        # A self-contained repeat is still about the same case, even if the
        # intent model treated its wording as an independent repair request.
        plan.follow_up = True
    # The intent model can occasionally over-attach a new, self-contained
    # fault to the active repair. Use a conservative subject check as a final
    # guard so a distinct topic starts and is answered as its own request.
    if (repair and repair['status'] not in ('resolved', 'unresolved') and not repair_action
            and plan.follow_up and clearly_new_topic(question, repair['issue'])):
        plan.follow_up = False
        plan.use_previous_photo = False
        plan.advance = False
        plan.topic = question
        plan.query = question
    if plan.revise_plan and repair and plan.follow_up and not repair_action:
        plan.repair = True
        repair['plan_version'] = repair.get('plan_version', 1) + 1
        repair.setdefault('reported_issues', []).append({
            'step_id': (repair.get('current_step') or {}).get('step_id', ''),
            'step_number': (repair.get('current_step') or {}).get('number', 0),
            'report': question, 'has_image': photo is not None})

    # A new upload normally starts a new subject. Do not let the planner carry
    # the previous topic into it unless the wording explicitly asks to continue.
    if photo is not None and plan.follow_up and not repair and not _explicit_follow_up_request(question):
        plan.follow_up = False
        plan.use_previous_photo = False
        plan.advance = False
        plan.guided = False

    if plan.repair and not repair_action and not plan.follow_up:
        if repair:
            if not hasattr(entry, 'cases'):
                entry.cases = {}
            entry.cases[repair['workflow_id']] = deepcopy(repair)
        # Route a distinct issue immediately. Keep the earlier workflow intact
        # in the entry until a new result is ready, so failures/clarifications
        # cannot discard its step or private cache.
        repair = None

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
            "- Please confirm the machine model or provide its matching manual.",
            documentable=False,
        )

        return response, False

    # ask the user for more details if needed
    if plan.clarify:
        metadata(outcome='clarification')
        return text_response(plan.clarify, documentable=False), False

    # reply if the question is outside the app's scope
    if plan.out_of_scope:
        metadata(outcome='out_of_scope')
        return text_response(cfg.UNAVAILABLE, documentable=False), False

    # Classify the current request, rather than inheriting guided mode from history.
    guided = bool(repair_action or plan.repair)
    entry.request_case_id = repair['workflow_id'] if repair and (guided or plan.follow_up) else ''
    if guided:
        start_safety(entry, question if not repair else repair['issue'])
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

    if repair and repair['status'] in ('resolved', 'unresolved'):
        repair = None
    request_step = guided and (repair is None or repair_action is not None or plan.revise_plan
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
        return text_response(cfg.UNAVAILABLE, documentable=False), False

    if guided and repair is None:
        repair = new_workflow(topic, bundle['chunks'])
        entry.request_case_id = repair['workflow_id']
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
        "ACTIVE_REPAIR_CONTEXT": ({'issue': repair['issue'], 'current_step': repair.get('current_step'),
                                   'completed_steps': repair['completed_steps'],
                                   'observations': repair.get('observations', [])}
                                  if repair and plan.follow_up else None),
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
        bundle["chunks"]
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
            "content": str(row.get("content", "")),
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

    # Keep every prepared figure for deterministic step assignment, the UI and
    # PDF. Gemini only needs the highest-ranked subset as pixels; source text
    # and metadata for every figure remain in APPROVED_EVIDENCE. This avoids
    # sending several megabytes of redundant images without hiding any output.
    model_images = [image for image in media['images']
                    if image['chunk_id'] in evidence_ids][:cfg.MODEL_MANUAL_IMAGE_LIMIT]
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
    metadata(payload_characters=len(parts[0].text), model_image_count=len(model_images),
             prepared_image_count=len(media['images']),
             model_image_bytes=(len(photo) if photo else 0) + model_image_bytes)
    # Payload building includes context assembly and attaching the selected image bytes.
    trace = current_trace.get()
    if trace:
        event = trace.start('payload_build', payload_started)
        trace.finish(event, payload_started, None, {})
    final_text = ""
    entry.request_stage = "answer generation"
    if getattr(entry, 'current_progress', None) is not None:
        entry.current_progress['stage'] = 'Updating the remaining steps' if repair_action else ('Preparing troubleshooting steps' if guided else 'Preparing the answer')
    generation_started = time.monotonic()
    logger.info("Chat stage started: answer generation")

    # Flash Lite supports the same grounded schema and multimodal input. Use it
    # for bounded requests, including guided repair, while retaining the larger
    # model for uploaded-photo analysis and unusually large evidence payloads.
    use_lite = (photo is None
                and sum(len(row['content']) for row in compact_chunks) <= 18000)
    selected_runner = lite_runner if use_lite else runner
    selected_model = cfg.FINAL_LITE_MODEL if use_lite else cfg.MODEL
    # run the agent with the question and supporting information
    increment('model_calls')
    usage_responses = 0
    try:
        with span('agent', model=selected_model) as model_measurements:
            async for event in selected_runner.run_async(
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
        if guided:
            await attach_safety(entry, repair, bundle)
        return _model_fallback_response(media, bundle, entry, guided, question, repair), False

    logger.info("Chat stage ended: answer generation (%.2fs)",
                time.monotonic() - generation_started)
    if guided:
        await attach_safety(entry, repair, bundle)
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
        return text_response(cfg.UNAVAILABLE, documentable=False), True

    if (not request_step and (answer.repair_step is not None or answer.remaining_repair_steps)):
        raise ValueError('A new repair step requires the current step Done confirmation')

    remaining_answers = []
    if request_step:
        if answer.repair_step is None:
            result = no_further_step(repair)
            entry.repair_workflow = repair
            return result, True
        seen = {_instruction_key(item['instruction']) for item in repair['completed_steps']}
        validate_branches([answer.repair_step] + answer.remaining_repair_steps)
        repair['total_steps'] = len(repair['completed_steps']) + 1 + len(answer.remaining_repair_steps)
        repair['conditional'] = any(step.branches for step in [answer.repair_step] + answer.remaining_repair_steps)
        if repair['conditional']:
            repair['total_steps'] = None
        allowed_assets = {item['id'] for item in media['images'] + media['links']}
        planned_steps = [answer.repair_step] + answer.remaining_repair_steps
        for planned in planned_steps:
            planned.instruction = _clean_repair_instruction(planned.instruction)
            key = _instruction_key(planned.instruction)
            if key in seen:
                raise ValueError('Model repeated a repair action in the procedure')
            seen.add(key)
            if set(planned.asset_ids) - allowed_assets:
                raise ValueError('Model supplied an unknown repair asset ID')
        # Associate same-page figures first, then allocate every other prepared
        # manual image to the closest supported step. No retrieved figure is
        # silently lost from the step-by-step UI or its generated PDF.
        _assign_all_step_images(planned_steps, bundle, media)
        plan_asset_ids = [identifier for planned in planned_steps
                          for identifier in planned.asset_ids]
        _ensure_step_image_descriptions(answer, plan_asset_ids, bundle, media)
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

    # Only expose assets explicitly associated with the visible action.
    if request_step:
        visible_assets = set(answer.repair_step.asset_ids)
        answer.relevant_asset_ids = list(visible_assets)
        answer.blocks = [block for block in answer.blocks if block.type not in ('image', 'link') or block.asset_id in visible_assets]

    # check that the session still exists
    await store.get(entry.id)

    # build the answer for the frontend
    with span('response_build'):
        result = final_blocks(answer, bundle, media, entry, True)
        # Validate future rendering now. Done must not discover a missing image
        # description, fabricated reference or duplicate instruction later.
        for future in remaining_answers:
            preview = future.model_copy(deep=True)
            preview_repair = deepcopy(repair)
            _apply_repair_step(preview, preview_repair)
            final_blocks(preview, bundle, media, entry, True)
    if guided:
        workflow_response(result, repair)
        if plan.revise_plan:
            result['plan_updated'] = True
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
                                 'plan_complete': True,
                                 'remaining_answers': remaining_answers,
                                 'wants_sources': True,
                                 'images_assigned_to_steps': True}
            metadata(repair_plan_step_count=1 + len(remaining_answers))

    # add warnings if some images or links could not be loaded
    result["asset_warnings"] = media["warnings"]

    return result, True
