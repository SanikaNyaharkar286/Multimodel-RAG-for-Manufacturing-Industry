# controls what happens after a technician asks a question, how the manuals are searched,
#  how the AI agent generates an answer, and how troubleshooting steps are handled.


import asyncio
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
from app.agent.coverage import EvidenceReview, review_evidence, merge_bundles, coverage_errors
from app.media.prepare_media import prepare_media
from app.media.manual_assets import image_identity, validate_reference
from app.responses.response_builder import text_response, final_blocks
from app.responses.schemas import AgentAnswer, AnswerBlock, ImageDescription, QueryPlan
from app.responses.step_validation import compound_instruction
from app.retrieval.manual_search import list_available_documents, search_manual
from app.sessions.session_store import session_service, store
from .conversation_context import latest_user_photo, recent_context
from .request_context import case_questions, clearly_new_topic, repeated_case_question
from .inventory import is_inventory_request, inventory_response
from .repair_workflow import (new_workflow, validate_action, resolve_report,
                              workflow_response, no_further_step)
from app.observability.timing import span, metadata, usage_metadata, increment, current_trace


# connects the agents to the session service
runner = Runner(agent=root_agent, app_name=cfg.APP_NAME, session_service=session_service)
lite_runner = Runner(agent=lite_agent, app_name=cfg.APP_NAME, session_service=session_service)
logger = logging.getLogger("uvicorn.error")


async def _generate_answer(selected_runner, selected_model, entry, parts):
    increment('model_calls')
    final_text = ''
    responses = 0
    with span('agent', model=selected_model) as measurements:
        async for event in selected_runner.run_async(
            user_id=entry.user_id, session_id=entry.id,
            new_message=types.Content(role='user', parts=parts),
            run_config=RunConfig(max_llm_calls=1),
        ):
            usage = getattr(event, 'usage_metadata', None)
            if usage is not None:
                if responses:
                    increment('model_calls')
                responses += 1
                measurements.update(usage_metadata(usage))
            if event.is_final_response() and event.content:
                final_text = ''.join(part.text for part in event.content.parts or []
                                     if part.text and not part.thought)
    return final_text



# removes text like "see section 5" from a repair step, so the step is a real action.
# if nothing is left, it makes an action from the step's purpose
def _clean_repair_instruction(instruction):
    """removes 'see section...' type text from a repair step."""
    original = instruction.strip()
    cleaned = re.sub(
        r"\s*(?:Refer to|See|Consult)\s+(?:the\s+)?(?:section|chapter|manual)\b.*$",
        "",
        original,
        flags=re.IGNORECASE,
    ).strip(" ;:\n")
    if cleaned:
        return cleaned
    # old saved plans may only say "refer to section".
    # turn that into a real action so it does not show in the ui or pdf
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


def _instruction_clauses(instruction):
    """Split independent action clauses without splitting component lists."""
    verbs = (r'inspect|check|verify|ensure|confirm|adjust|align|install|remove|replace|'
             r'clean|lubricate|calibrate|tighten|loosen|press|connect|disconnect|'
             r'refill|fill|drain|start|stop|raise|lower|release|clamp|unclamp|'
             r'measure|test|balance|dress|select|enter|reset|switch')
    text = re.sub(r'\s+', ' ', str(instruction).strip())
    clauses = re.split(
        rf'\s+(?:(?:and|then)\s+|[,;]\s+)(?=(?:also\s+)?(?:{verbs})\b)',
        text, flags=re.IGNORECASE,
    )
    if len(clauses) == 1:
        clauses = re.split(rf'(?<=[.;])\s+(?=(?:also\s+)?(?:{verbs})\b)',
                           text, flags=re.IGNORECASE)
    return [clause.strip(' ;,.') for clause in clauses if clause.strip(' ;,.')]


def _split_checklist_actions(answer, review):
    """Normalize a model step that accidentally combines independent actions.

    The coverage validator intentionally rejects compound guided steps. Models
    can still occasionally place two checklist action IDs on one step, so
    separate those actions deterministically from the approved checklist
    before validation. This is machine-agnostic and keeps the guided UI
    one-action-at-a-time without asking the model for another expensive call.
    """
    action_items = {item.id: item for item in review.items if item.kind == 'action'}
    planned = ([answer.repair_step] if answer.repair_step else []) + list(answer.remaining_repair_steps)
    if not planned:
        return answer

    normalized_steps = []
    changed = False
    for step in planned:
        action_ids = [identifier for identifier in step.coverage_ids if identifier in action_items]
        if len(action_ids) <= 1 and not compound_instruction(step.instruction):
            normalized_steps.append(step)
            continue
        # Only split when the independent checklist already gives us the
        # authoritative actions. If it does not, let normal validation report
        # the problem rather than inventing a procedure from free text.
        if len(action_ids) <= 1:
            clauses = _instruction_clauses(step.instruction)
            if len(clauses) <= 1:
                normalized_steps.append(step)
                continue
            changed = True
            for index, clause in enumerate(clauses):
                split_step = step.model_copy(deep=True)
                split_step.step_key = f'{step.step_key}-{index + 1}' if step.step_key else f'action-{index + 1}'
                split_step.title = clause[:160]
                split_step.instruction = _clean_repair_instruction(clause)
                # With coverage review disabled there is no authoritative
                # per-action mapping, so preserve all approved supporting
                # details on each prepared step rather than dropping context.
                split_step.supporting_blocks = [block.model_copy(deep=True)
                                                for block in step.supporting_blocks]
                split_step.asset_ids = list(step.asset_ids)
                split_step.image_descriptions = [description.model_copy(deep=True)
                                                 for description in step.image_descriptions]
                if index < len(clauses) - 1:
                    split_step.branches = []
                normalized_steps.append(split_step)
            continue
        changed = True
        for index, identifier in enumerate(action_ids):
            item = action_items[identifier]
            split_step = step.model_copy(deep=True)
            split_step.step_key = f'{step.step_key}-{index + 1}' if step.step_key else f'action-{index + 1}'
            split_step.coverage_ids = [identifier]
            split_step.title = item.description[:160]
            split_step.instruction = _clean_repair_instruction(item.description)
            # Keep content tied to the action. Blocks without coverage IDs are
            # retained on the first split step so no explanatory evidence is
            # silently discarded.
            split_step.supporting_blocks = [
                block.model_copy(deep=True)
                for block in step.supporting_blocks
                if identifier in block.coverage_ids or (not block.coverage_ids and index == 0)
            ]
            split_assets = {block.asset_id for block in split_step.supporting_blocks
                            if block.type in ('image', 'link') and block.asset_id}
            split_step.asset_ids = [asset for asset in step.asset_ids
                                   if asset in split_assets or (index == 0 and asset not in {
                                       block.asset_id for block in step.supporting_blocks
                                       if block.asset_id})]
            split_step.image_descriptions = [description.model_copy(deep=True)
                                             for description in step.image_descriptions
                                             if description.asset_id in split_step.asset_ids]
            # Observation branches belong to the action that originally held
            # them; keep them on the final split action to preserve routing.
            if index < len(action_ids) - 1:
                split_step.branches = []
            normalized_steps.append(split_step)

    if not changed:
        return answer
    answer.repair_step = normalized_steps[0]
    answer.remaining_repair_steps = normalized_steps[1:]
    return answer


# Preserve explicit model assignments. A shared PDF page (especially virtual
# page 1 in stitched manuals) cannot establish which step a figure illustrates.
def _step_asset_ids(step, bundle, media):
    """Keep selected assets and add approved citations for this action.

    Manual links are evidence assets too. Models can omit them from a guided
    step even after selecting the supporting evidence, so attach only links
    whose indexed chunk is cited by this step. This keeps citations complete
    without leaking links from another troubleshooting step.
    """
    block_assets = [block.asset_id for block in step.supporting_blocks
                    if block.type in ('image', 'link') and block.asset_id]
    evidence_ids = set(step.evidence_ids)
    for block in step.supporting_blocks:
        evidence_ids.update(block.evidence_ids)
    cited_links = [item['id'] for item in media.get('links', [])
                   if item.get('chunk_id') in evidence_ids]
    return list(dict.fromkeys(step.asset_ids + block_assets + cited_links))


# A missing visual interpretation must remain explicit. Nearby manual text
# can describe another figure, so it cannot stand in for inspected image data.
def _ensure_step_image_descriptions(answer, asset_ids, bundle, media):
    """Fill omitted descriptions without guessing visible parts or callouts."""
    described = {item.asset_id for item in answer.image_descriptions}
    images = {item['id']: item for item in media.get('images', [])}
    evidence = {row.get('chunk_id'): row for row in bundle.get('chunks', [])}
    for identifier in asset_ids:
        if identifier in described or identifier not in images:
            continue
        image = images[identifier]
        source = evidence.get(image.get('chunk_id'), {})
        source_text = re.sub(r'\s+', ' ', str(source.get('content', '')).strip())
        source_text = re.sub(
            r'(?i)^(?:machine|document|page|image id|link id):\s*[^.]+[.]\s*',
            '', source_text,
        ).strip()
        if len(source_text) > 520:
            source_text = source_text[:517].rsplit(' ', 1)[0] + '...'
        description_text = source_text or 'The original manual figure associated with this approved evidence.'
        description = (
            f'- Description: {description_text}\n'
            '- Technician check: Compare this figure with the manual-supported action: '
            f'{getattr(getattr(answer, "repair_step", None), "instruction", "") or "Identify the relevant component using the supplied manual evidence."}'
        )
        answer.image_descriptions.append(
            ImageDescription(asset_id=identifier, description=description))
        described.add(identifier)


# Keep media on the step selected after inspecting the actual images. Never
# assign unselected figures by page distance or duplicate them across all steps.
def _assign_all_step_images(steps, bundle, media):
    """Normalize explicit per-step selections without adding page neighbours."""
    for step in steps:
        step.asset_ids = _step_asset_ids(step, bundle, media)


# backup when the model is down: reads the repair actions written in the manual text
# ("corrective action: ...") and skips the ones already done
def _fallback_manual_actions(bundle, completed_steps):
    """pulls the repair actions written in the manual text, skipping done ones."""
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
            if not instruction or compound_instruction(instruction):
                continue
            key = _instruction_key(instruction)
            if key and key not in seen:
                seen.add(key)
                actions.append((instruction[:1200], row))
    return actions


# backup when the model is down: gives every manual image to the closest backup action
def _fallback_step_assets(actions, bundle, media):
    """Fallback can associate media only with the action's own evidence."""
    return [[asset['id'] for asset in media.get('images', []) + media.get('links', [])
             if asset.get('chunk_id') == row.get('chunk_id')]
            for _, row in actions]


# backup answer when the final model fails or times out.
# for a repair it shows the first step written in the manual (the other steps stay hidden for Done).
# otherwise it shows a few manual excerpts
def _model_fallback_response(media, bundle, entry, guided, question='', workflow=None):
    """gives a backup answer from the manual when the model is not working."""
    blocks = []
    repair = workflow or getattr(entry, 'repair_workflow', None)
    # if the final answer timed out but the user asked for a fix,
    # still show repair steps and not a raw dump of text
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
            # keep the later actions hidden so Done can move on
            # without another slow model call or a new search
            remaining = [
                {'instruction': action, 'chunk_id': row.get('chunk_id', ''),
                 'asset_ids': action_assets[index]}
                for index, (action, row) in enumerate(actions[1:], start=1)
            ]
            metadata(fallback_step=True, fallback_evidence_id=source.get('source_id', ''))
        else:
            # A timeout or provider failure may leave the manual evidence
            # without a literal "CORRECTIVE ACTION:" label. For a new case,
            # that does not mean the procedure is finished. Show the approved
            # excerpts and all prepared manual figures instead of displaying
            # "No further supported repair step".
            if repair.get('completed_steps'):
                result = no_further_step(repair)
                entry.repair_workflow = repair
                return result
            repair = None
            guided = False
            message = "The answer service could not complete the structured response. Relevant approved manual information is shown below."
            excerpts = []
            for row in bundle.get('chunks', [])[:8]:
                content = re.sub(r'\s+', ' ', str(row.get('content', '')).strip())
                if content:
                    excerpts.append(content[:1800])
            if excerpts:
                message += "\n\n### Relevant manual information\n\n" + "\n\n---\n\n".join(excerpts)
            blocks.append({'type': 'text', 'content': message})
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
                    if not (guided and repair) or asset.get('id') in visible_fallback_ids]
    seen_image_digests = set()
    for asset in image_assets:
        digest = image_identity(asset)
        if digest in seen_image_digests:
            continue
        seen_image_digests.add(digest)
        asset_id = store.put_media_image(entry, asset)
        blocks.append({
            "type": "image",
            "src": f"/images/{asset_id}",
            "caption": f"Original diagram — {asset.get('document', 'approved manual')}, PDF page {asset.get('page', 'unknown')}.",
            "description": (
                f"- Description: It supports Step {number}.\n"
                f"- Technician check: {instruction}."
                if guided and repair
                else "- Description: It supports the requested manual answer."
            ),
            "source": {"document_name": asset.get('document'),
                       "page_number": asset.get('page')},
        })
    result = {"blocks": blocks, "degraded": True, "documentable": False}
    for link in media.get('links', []):
        if guided and repair and link['id'] not in visible_fallback_ids:
            continue
        blocks.append({'type': 'link', 'url': link['url'],
                       'title': 'Watch related video' if link.get('is_video') else 'Open manual reference',
                       'isVideo': link.get('is_video', False), 'origin': link.get('origin', 'pdf_link')})
    if guided and repair:
        repair['coverage_verified'] = False
        entry.repair_workflow = repair
        cache = getattr(entry, 'repair_cache', None) or {}
        entry.repair_cache = {**cache, 'bundle': bundle, 'media': media,
                              'workflow_id': repair['workflow_id'],
                              'plan_complete': False, 'plan_ready': True, 'remaining_answers': remaining,
                              'wants_sources': cache.get('wants_sources', False),
                              'images_assigned_to_steps': True}
        workflow_response(result, repair)
    return result


# true only when the user clearly asks to continue the last task, like "next step" or "continue"
def _explicit_follow_up_request(question):
    """true only when the user clearly asks to continue the last task."""
    text = re.sub(r"[^a-z0-9 ]+", " ", question.lower())
    return bool(re.search(
        r"\b(next step|next|continue|what should i check now|what do i check next|why that|why this check|then what)\b",
        text,
    ))


# answers hello, thanks, "who are you" and "what can you do" type questions
# directly, without searching the manuals. gives None for any other question
def _smalltalk_reply(question):
    """answers hello, thanks and 'who are you' type questions without searching the manuals."""
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


# makes a simple lowercase key from a step, so we can find repeated steps
def _instruction_key(instruction):
    return re.sub(r'[^\w]+', ' ', _clean_repair_instruction(instruction).casefold()).strip()


def _cache_ready(cached):
    if not cached or not cached.get('plan_ready', cached.get('plan_complete')):
        return False
    for item in cached.get('remaining_answers', []):
        if not isinstance(item, dict) and item.repair_step is None:
            return False
        instruction = item.get('instruction', '') if isinstance(item, dict) else item.repair_step.instruction
        if compound_instruction(instruction):
            return False
    return True


def _corrective_action_rows(rows, question, limit=None):
    """Return the manual rows most useful for concrete corrective steps."""
    query_terms = set(re.findall(r"[a-z0-9-]+", str(question).casefold()))
    query_terms -= {"what", "why", "how", "the", "is", "my", "are", "of", "to", "and", "a", "for",
                    "show", "image", "images", "relevant", "explain", "should", "inspect", "check",
                    "replace", "give", "help", "with", "please", "step", "steps", "by", "it", "me"}
    action_terms = {
        # Generic maintenance verbs and component/measurement terms.  The
        # actual machine vocabulary comes from query_terms and the manual;
        # nothing here assumes a particular machine or manufacturer.
        "inspect", "inspection", "check", "checking", "verify", "verification",
        "measure", "measurement", "test", "testing", "adjust", "adjustment",
        "set", "setting", "align", "alignment", "install", "installation",
        "remove", "replace", "replacement", "clean", "cleaning", "lubricate",
        "lubrication", "calibrate", "calibration", "tighten", "loosen",
        "pressure", "level", "temperature", "flow", "filter", "sensor",
        "bearing", "motor", "belt", "shaft", "wheel", "guard", "valve",
        "switch", "fuse", "oil", "coolant", "leak", "noise", "vibration",
    }
    ranked = []
    for index, row in enumerate(rows):
        content = re.sub(r"\s+", " ", str(row.get("content", "")).strip())
        lowered = content.casefold()
        if not content:
            continue
        words = set(re.findall(r"[a-z0-9-]+", lowered))
        overlap = len(query_terms.intersection(words))
        action_overlap = len(action_terms.intersection(words))
        # Headings and explicit procedure labels are useful for every manual,
        # regardless of whether the equipment is a saw, grinder, pump, or
        # another machine.
        section_hits = len(re.findall(
            r"(?i)\b(?:procedure|installation|replacement|adjustment|"
            r"calibration|maintenance|troubleshooting|inspection|operation)\b",
            lowered,
        ))
        numbered_actions = len(re.findall(r"(?:^|\s)\d+\s+(?:[A-Z]|Note:)", content))
        if overlap and (action_overlap or section_hits or row.get('content_type') == 'table'):
            ranked.append((
                -(overlap * 12 + section_hits * 2 + action_overlap + numbered_actions),
                index,
                row,
            ))
    ranked.sort(key=lambda item: (item[0], item[1]))
    return [row for _, _, row in ranked[:limit]]


def _model_evidence_chunks(bundle, media, question, required_ids=()):
    """Keep the complete retrieval bundle without text/table/image count cuts."""
    rows = bundle['chunks']
    actions = _corrective_action_rows(rows, question)
    # Transport/caching optimizations must never shorten the retrieved evidence.
    # Keep row order, full tables and every reference regardless of old budgets.
    return list(rows), actions, sum(len(str(row.get('content', ''))) for row in rows)


# sets the checked repair step as the current step and keeps the future steps hidden.
# it raises an error if the model repeats a step that is already done
def _apply_repair_step(answer, repair, check_observations=True):
    """shows one checked repair step and keeps the future steps hidden."""
    instruction = _clean_repair_instruction(answer.repair_step.instruction)
    if compound_instruction(instruction):
        raise ValueError('Split independent repair actions into separate steps')
    for prerequisite in (answer.repair_step.requires_observations if check_observations else []):
        if not any(item.get('step_key') == prerequisite.step_key
                   and item.get('observation') in prerequisite.allowed_observations
                   for item in repair['completed_steps']):
            raise ValueError('A required observed result has not been confirmed for this action')
    if any(_instruction_key(item['instruction']) == _instruction_key(instruction)
           for item in repair['completed_steps']):
        raise ValueError('Model repeated an already completed repair step')
    number = len(repair['completed_steps']) + 1
    repair['current_step'] = {'step_id': str(uuid4()), 'number': number,
                              'instruction': instruction,
                              **answer.repair_step.model_dump(exclude={
                                  'instruction', 'evidence_ids', 'asset_ids',
                                  'supporting_blocks', 'image_descriptions', 'coverage_ids'})}
    repair['current_step']['expected_result'] = answer.repair_step.expected_result or 'A specific expected result is not recorded in the retrieved evidence. Record what you observe.'
    repair['status'] = 'awaiting_done'
    assets = set(answer.repair_step.asset_ids)
    details = list(answer.repair_step.supporting_blocks)
    selected = {block.asset_id for block in details if block.asset_id}
    # Legacy image captions may live in top-level blocks. Keep only the
    # current step's media; future supporting text/tables remain private.
    details.extend(block for block in answer.blocks
                   if block.type in ('image', 'link') and block.asset_id in assets
                   and block.asset_id not in selected)
    answer.relevant_asset_ids = list(answer.repair_step.asset_ids)
    descriptions = {item.asset_id: item for item in answer.image_descriptions}
    descriptions.update({item.asset_id: item for item in answer.repair_step.image_descriptions})
    answer.image_descriptions = [item for identifier, item in descriptions.items() if identifier in assets]
    answer.blocks = [AnswerBlock(type='text', content=f'### Step {number}\n{instruction}',
                                 evidence_ids=answer.repair_step.evidence_ids)] + details


def _step_answer(answer, step):
    """Prepare one self-contained answer without carrying future step content."""
    result = answer.model_copy(deep=True)
    result.repair_step = step.model_copy(deep=True)
    result.remaining_repair_steps = []
    result.relevant_asset_ids = list(step.asset_ids)
    result.blocks = [block for block in answer.blocks
                     if block.type in ('image', 'link') and block.asset_id in step.asset_ids]
    # The selected step may override a shared image's technician check.
    descriptions = {item.asset_id: item for item in answer.image_descriptions}
    descriptions.update({item.asset_id: item for item in step.image_descriptions})
    result.image_descriptions = [item for identifier, item in descriptions.items()
                                 if identifier in step.asset_ids]
    return result


# checks that step keys are unique and that every branch goes to a later step or END
def validate_branches(steps):
    keys = [step.step_key for step in steps]
    if any(keys) and len([key for key in keys if key]) != len(set(key for key in keys if key)):
        raise ValueError('Duplicate plan step key')
    for index, step in enumerate(steps):
        for branch in step.branches:
            if branch.next_step_key != 'END' and branch.next_step_key not in keys[index + 1:]:
                raise ValueError('Branch must target a later prepared step or END')


# waits for the safety warnings and adds them to the repair, with the manual pages that mention safety
async def attach_safety(entry, repair, bundle):
    if repair and getattr(entry, 'safety_task', None):
        repair['safety'] = await entry.safety_task
        repair['safety']['sources'] = [
            {'document_name': row['document_name'], 'page_number': row['page_number']}
            for row in bundle['chunks'] if re.search(r'safety|lockout|isolate|warning', str(row.get('content', '')), re.I)]


def cached_step_previews(entry):
    """Read-only previews for the current Done choices, from validated results.

    These are transport data only. Completion and observations are still
    validated and recorded by the normal authenticated POST /chat action.
    """
    repair = getattr(entry, 'repair_workflow', None)
    cached = getattr(entry, 'repair_cache', None)
    if (not repair or repair.get('status') != 'awaiting_done' or not cached
            or cached.get('workflow_id') != repair.get('workflow_id')
            or not _cache_ready(cached)):
        return []
    current = repair.get('current_step') or {}
    choices = current.get('branches') or [{'label': '', 'next_step_key': ''}]
    remaining = cached.get('remaining_answers', [])
    previews = []
    for choice in choices:
        target = choice.get('next_step_key', '')
        candidate = None
        if target != 'END':
            candidate = (next((item for item in remaining if not isinstance(item, dict)
                              and item.repair_step.step_key == target), None)
                         if target else next(iter(remaining), None))
            if target and candidate is None:
                continue
        draft = deepcopy(repair)
        if not cached.get('coverage_version'):
            draft['coverage_verified'] = False
        draft['completed_steps'].append({**deepcopy(current), 'observation': choice['label']})
        draft['current_step'] = None
        if candidate is None:
            result = no_further_step(draft)
        else:
            if isinstance(candidate, dict):
                continue  # Older plans use the authoritative server path.
            prepared = cached.get('prepared_results', {}).get(
                _instruction_key(candidate.repair_step.instruction))
            if prepared is None:
                continue
            _apply_repair_step(candidate.model_copy(deep=True), draft)
            result = deepcopy(prepared)
            result['blocks'][0]['content'] = (
                f'### Step {draft["current_step"]["number"]}\n{draft["current_step"]["instruction"]}')
            workflow_response(result, draft)
        previews.append({'workflow_id': repair['workflow_id'], 'step_id': current['step_id'],
                         'plan_version': repair.get('plan_version', 1),
                         'observation': choice['label'], 'response': result})
    return previews


# moves to the next step when the technician presses Done.
# it uses the plan that was saved earlier, so there is no new model call, search or image download
async def _advance_cached_plan(entry, repair, cached):
    """moves to the next step from the saved plan. no model, search or image download needed."""
    with span('repair_plan_advance'):
        if not cached.get('coverage_version'):
            repair['coverage_verified'] = False
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
                selected_ids = set(planned.get('asset_ids', []))
                for asset in cached['media'].get('images', []):
                    # The step already has its approved asset IDs. Do not add
                    # every figure from the same PDF page; stitched manuals
                    # commonly place unrelated figures on virtual page 1.
                    if asset.get('id') not in selected_ids:
                        continue
                    digest = image_identity(asset)
                    if digest in shown:
                        continue
                    shown.add(digest)
                    asset_id = store.put_media_image(entry, asset)
                    document = asset.get('document', source.get('document_name', 'the approved manual'))
                    page = asset.get('page', source.get('page_number'))
                    result['blocks'].append({
                        'type': 'image', 'src': f'/images/{asset_id}',
                        'caption': f'Original diagram — {document}, PDF page {page}.',
                        'description': (
                            f'- Description: It supports Step {number}.\n'
                            f'- Technician check: {instruction}.'
                        ),
                        'source': {'document_name': document, 'page_number': page},
                    })
                for link in cached['media'].get('links', []):
                    if link['id'] in selected_ids:
                        result['blocks'].append({
                            'type': 'link', 'url': link['url'],
                            'title': 'Watch related video' if link.get('is_video') else 'Open manual reference',
                            'isVideo': link.get('is_video', False), 'origin': link.get('origin', 'pdf_link')})
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
                prepared = cached.get('prepared_results', {}).get(
                    _instruction_key(answer.repair_step.instruction))
                result = deepcopy(prepared) if prepared is not None else final_blocks(
                    answer, cached['bundle'], cached['media'], entry, cached['wants_sources'])
                if prepared is not None:
                    # Branches may skip actions, so assign the visible number
                    # when releasing the already-rendered content.
                    result['blocks'][0]['content'] = (
                        f'### Step {repair["current_step"]["number"]}\n'
                        f'{repair["current_step"]["instruction"]}')
            workflow_response(result, repair)
        else:
            result = no_further_step(repair)
        # save only after the reply is built and stored
        # The live workflow and cached plan are already updated in memory.
        # Persist the ADK state in the background so the Done response does not
        # wait for multiple remote session round trips.
        async def persist_step_state():
            try:
                await store.update_state(entry, {
                    'main_topic': repair['issue'],
                    'guided': True,
                    'step_number': len(repair['completed_steps']) + bool(remaining),
                    'finished': False,
                })
            except Exception as exc:
                logger.warning('Cached repair state update failed error_type=%s', type(exc).__name__)
        asyncio.create_task(persist_step_state())
        if remaining:
            remaining.pop(0)
        cached['remaining_answers'] = remaining
        entry.repair_workflow = repair
        result['asset_warnings'] = cached['media']['warnings']
        increment('cache_hits')
        metadata(outcome='answered' if remaining or repair['current_step'] else 'completed',
                 repair_plan_cache_hit=True)
        # The route already records the user/assistant history. Returning
        # runner=True avoids the duplicate direct-reply ADK save on Done.
        return result, True


# main function. it plans the search and gets an answer for the user's question.
# order: handle repair buttons -> catalog and small talk -> understand the question
# -> search manuals -> load images -> ask the agent -> check the answer -> build the reply
async def answer_question(entry, question, photo, repair_action=None, start_new_case=False):
    # runs one step in a thread, saves its name for progress, and logs its time
    async def stage(name, function, *args):
        entry.request_stage = name
        if getattr(entry, 'current_progress', None) is not None:
            entry.current_progress['stage'] = {
                'manual evidence retrieval': 'Searching the manuals',
                'manual coverage review': 'Checking relevant manual details',
                'manual image and link loading': 'Loading relevant manual figures',
                'question analysis': 'Classifying your request',
                'image and question analysis': 'Analysing the uploaded image',
            }.get(name, name)
        started = time.monotonic()
        logger.info("Chat stage started: %s", name)
        try:
            key = {'manual inventory lookup': 'inventory',
                   'manual coverage review': 'coverage_preparation',
                   'image and question analysis': 'planner', 'question analysis': 'planner',
                   'manual evidence retrieval': 'retrieval',
                   'manual image and link loading': 'media'}[name]
            with span(key):
                return await asyncio.to_thread(function, *args)
        finally:
            logger.info("Chat stage ended: %s (%.2fs)", name, time.monotonic() - started)

    repair = deepcopy(getattr(entry, 'repair_workflow', None))
    if getattr(entry, 'repair_cache', None) and not _cache_ready(entry.repair_cache):
        # Preserve old conversations, but rebuild obsolete multi-action plans
        # rather than failing snapshot decoding or releasing an invalid step.
        if entry.repair_cache.get('plan_ready', entry.repair_cache.get('plan_complete')):
            entry.repair_cache = None
    # Done advances a validated cached plan. Handle it before reading the ADK
    # session state; the next step and media are already in memory.
    if (repair_action and repair_action.action == 'done' and repair
            and repair.get('workflow_id')
            and getattr(entry, 'repair_cache', None)
            and entry.repair_cache.get('workflow_id') == repair['workflow_id']
            and _cache_ready(entry.repair_cache)):
        repair = validate_action(repair, repair_action)
        return await _advance_cached_plan(entry, repair, entry.repair_cache)

    # get the saved details for this session
    state = await store.state(entry)
    if start_new_case and repair:
        entry.cases[repair['workflow_id']] = deepcopy(repair)
        repair = None
        entry.repair_workflow = None
        entry.repair_cache = None
        state = {}
    if repair and repair['status'] not in ('resolved', 'unresolved'):
        # keep a paused repair separate from the latest info topic
        state = {**state, 'active_repair': {'issue': repair['issue'],
                 'machine': repair.get('machine', ''),
                 'current_step': repair.get('current_step'),
                 'original_questions': case_questions(entry.history, repair['workflow_id'])}}
    if repair_action:
        repair = validate_action(repair, repair_action)
        if repair_action.action == 'report_issue':
            # the reported result can make all saved next steps wrong
            entry.repair_cache = None
        if repair_action.action in ('resolve', 'unresolved'):
            result = resolve_report(repair, repair_action)
            await store.update_state(entry, {'guided': False, 'finished': True})
            entry.repair_workflow = repair
            entry.repair_cache = None
            return result, False
        cached = getattr(entry, 'repair_cache', None)
        if (repair_action.action == 'done' and cached and cached.get('workflow_id') == repair['workflow_id']
                and _cache_ready(cached)):
            return await _advance_cached_plan(entry, repair, cached)

    # lists the manuals that are available
    async def answer_inventory():
        documents = await stage("manual inventory lookup", list_available_documents)
        metadata(outcome='inventory')
        return text_response(inventory_response(documents)), False

    # "what manuals do you have" needs a full list, not a search
    if repair_action is None and photo is None and is_inventory_request(question):
        return await answer_inventory()

    # greetings and questions about the copilot are not manual searches.
    # answer them directly
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
    # typing "next" cannot confirm the work is done. only the Done button moves on
    plan.advance = repair_action is not None
    if (repair and repair['status'] not in ('resolved', 'unresolved') and not repair_action
            and repeated_case_question(question, repair['issue'],
                                       case_questions(entry.history, repair['workflow_id']))):
        # a repeated question is still the same case
        plan.follow_up = True
    # the planner sometimes ties a new fault to the old repair.
    # this check starts a new request if the topic is clearly different
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

    # a new photo usually means a new topic,
    # unless the user clearly asks to continue
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
        # start the new issue now, but keep the old repair saved
        # until the new result is ready
        repair = None

    # the planner can also spot manual list requests
    if plan.intent == 'document_inventory':
        return await answer_inventory()

    # use the earlier photo only if the planner chose it
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

    # decide guided mode from this request, not from old history
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

    # add the main topic to a follow-up search if needed
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
            # Guided repair steps always need the original extracted figures,
            # even when the technician did not explicitly type "show image".
            guided or plan.wants_visual,
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


    coverage_review = EvidenceReview(review_complete=False)
    coverage_review_failed = False
    if cfg.MANUAL_COVERAGE_REVIEW_ENABLED and (request_step or not guided):
        try:
            coverage_review = await stage('manual coverage review', review_evidence,
                query_text, bundle, guided, repair.get('completed_steps', []) if repair else [])
            if coverage_review.additional_searches:
                if not coverage_review.machine_names:
                    raise ValueError('Additional searches require an evidence-backed machine scope')
                expanded = await asyncio.gather(*[
                    stage('manual evidence retrieval', search_manual,
                          item.query, guided or plan.wants_visual, plan.wants_video,
                          tuple(coverage_review.machine_names))
                    for item in coverage_review.additional_searches])
                bundle = merge_bundles([bundle, *expanded])
                coverage_review = await stage('manual coverage review', review_evidence,
                    query_text, bundle, guided, repair.get('completed_steps', []) if repair else [], False)
            if not coverage_review.review_complete:
                coverage_review_failed = True
                logger.warning('Continuing from approved evidence with a partial coverage review')
            if coverage_review.machine_names:
                chosen = set(coverage_review.machine_names)
                chunks = [row for row in bundle['chunks'] if row.get('machine_name') in chosen]
                chunk_ids = {row['chunk_id'] for row in chunks}
                bundle = {**bundle, 'chunks': chunks,
                          'images': [item for item in bundle['images'] if item['chunk_id'] in chunk_ids],
                          'links': [item for item in bundle['links'] if item['chunk_id'] in chunk_ids]}
        except (APIError, TimeoutError, RequestsReadTimeout, httpx.ReadTimeout,
                DeadlineExceeded, ValueError) as error:
            logger.warning('Manual coverage review unavailable error_type=%s reason_code=%s; continuing from approved evidence',
                           type(error).__name__, getattr(error, 'code', 'review_unavailable'))
            # Keep any earlier validated checklist items. Invalid audit output
            # is discarded, while the primary answer still gets the full
            # retrieved passages, real figures and normal grounding checks.
            coverage_review.review_complete = False
            coverage_review.additional_searches = []
            coverage_review_failed = True

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

    model_chunks, corrective_rows, model_characters = _model_evidence_chunks(
        bundle, media, query_text,
        {identifier for item in coverage_review.items for identifier in item.evidence_ids})
    metadata(model_chunk_count=len(model_chunks), model_context_characters=model_characters)

    # the model only needs short text and source info.
    # extra data wastes tokens and time, so we remove it
    compact_chunks = []
    for row in model_chunks:
        compact = {
            "chunk_id": row["chunk_id"],
            "content": str(row.get("content", "")),
            "document_name": row.get("document_name", ""),
            "machine_name": row.get("machine_name", ""),
            "page_number": row.get("page_number"),
            "source_id": row.get("source_id", ""),
            "content_type": row.get("content_type", ""),
        }
        compact_chunks.append(compact)
    compact_by_id = {row["chunk_id"]: row for row in compact_chunks}

    # give the evidence short ids for this request
    evidence_ids = {
        row["chunk_id"]: f"evidence-{index + 1}"
        for index, row in enumerate(model_chunks)
    }
    source_ids = {value: key for key, value in evidence_ids.items()}

    # Every semantically retrieved figure is available for actual inspection;
    # there is no fixed count cap on model input or required displayed figures.
    model_images = [image for image in media['images']
                    if image['chunk_id'] in evidence_ids]
    model_image_bytes = sum(len(image.get('bytes', b'')) for image in model_images)

    image_information = [
        {
            **{key: value for key, value in image.items() if key not in {'bytes', 'reference'}},
            "chunk_id": evidence_ids[image["chunk_id"]],
            "source_chunk_ids": [evidence_ids[key] for key in image.get('source_chunk_ids', [image['chunk_id']])
                                 if key in evidence_ids],
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
        "EVIDENCE_CHECKLIST": {
            'enabled': cfg.MANUAL_COVERAGE_REVIEW_ENABLED,
            'review_complete': coverage_review.review_complete,
            'machine_names': coverage_review.machine_names,
            'items': [{**item.model_dump(),
                       'evidence_ids': [evidence_ids[identifier] for identifier in item.evidence_ids]}
                      for item in coverage_review.items],
        },
        "REQUIRED_STEP_OUTLINE": [
            {'coverage_id': item.id, 'action': item.description,
             'evidence_ids': [evidence_ids[identifier] for identifier in item.evidence_ids],
             'requires_pass_of': item.requires_pass_of,
             'observation_required': item.observation_required}
            for item in coverage_review.items if item.kind == 'action'
        ] if request_step else [],
        "recent_context": recent,
        "APPROVED_EVIDENCE": [
            {**row, "chunk_id": evidence_ids[row["chunk_id"]]}
            for row in compact_chunks
        ],
        "REQUIRED_CORRECTIVE_ACTION_EVIDENCE": [
            {"chunk_id": evidence_ids[row["chunk_id"]]}
            for row in corrective_rows
            if row["chunk_id"] in evidence_ids
        ],
        "allowed_images": image_information,
        "allowed_links": [
            {**link, "chunk_id": evidence_ids[link["chunk_id"]]}
            for link in media["links"]
            if link["chunk_id"] in evidence_ids
        ],
        "asset_load_warnings": media["warnings"],
    }

    # A stable full-manual prefix lets provider implicit context caching reuse
    # repeated evidence. Dynamic questions/workflow follow it, without summaries.
    prefix_keys = ('APPROVED_EVIDENCE', 'allowed_images', 'allowed_links')
    payload = {**{key: payload[key] for key in prefix_keys},
               **{key: value for key, value in payload.items() if key not in prefix_keys}}

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
                types.Part.from_uri(file_uri=validate_reference(image['reference'])['uri'],
                                    mime_type=image['reference']['mime_type'])
                if image.get('reference') else types.Part.from_bytes(data=image['bytes'], mime_type='image/png'),
            ]
        )

    # prepare a place for the agent's final answer
    metadata(payload_characters=len(parts[0].text), model_image_count=len(model_images),
             model_image_uri_count=sum(bool(image.get('reference')) for image in model_images),
             prepared_image_count=len(media['images']),
             model_image_bytes=(len(photo) if photo else 0) + model_image_bytes)
    # payload time includes building the context and adding the image bytes
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

    # the lite model uses the same format and accepts images too.
    # use it for normal-size requests. use the bigger model for photos and very large evidence
    use_lite = (photo is None
                and sum(len(row['content']) for row in compact_chunks) <= 18000)
    selected_runner = lite_runner if use_lite else runner
    selected_model = cfg.FINAL_LITE_MODEL if use_lite else cfg.MODEL
    # run the agent with the question and supporting information
    try:
        errors = []
        for attempt in range(2):
            request_parts = parts if not attempt else [*parts, types.Part(text=json.dumps({
                'REPAIR_DRAFT': final_text, 'VALIDATION_ERRORS': errors,
                'instruction': 'Return the corrected complete AgentAnswer using only the supplied evidence. '
                    'Cover every checklist item, split independent actions, preserve original tables and '
                    'all exact details, and prepare observation branches. Do not omit an item to shorten the answer. '
                    'For every table, return at least one column and make every row contain exactly one value '
                    'for each column; if a table cannot be represented safely, return its complete contents as '
                    'an evidence-backed text block instead of a malformed table.',
            }, ensure_ascii=False))]
            final_text = await _generate_answer(selected_runner, selected_model, entry, request_parts)
            try:
                answer = AgentAnswer.model_validate_json(final_text)
                if request_step:
                    answer = _split_checklist_actions(answer, coverage_review)
                steps = ([answer.repair_step] if answer.repair_step else []) + answer.remaining_repair_steps
                if request_step:
                    validate_branches(steps)
                for block in answer.blocks + steps + [block for step in steps for block in step.supporting_blocks]:
                    if any(identifier not in source_ids for identifier in block.evidence_ids):
                        raise ValueError('Model supplied an unknown evidence ID')
                    block.evidence_ids = [source_ids[identifier] for identifier in block.evidence_ids]
                errors = (coverage_errors(answer, coverage_review, bool(request_step),
                          repair.get('completed_steps', []) if repair else [], media,
                          require_review=cfg.MANUAL_COVERAGE_REVIEW_ENABLED and not coverage_review_failed) if answer.supported
                          else ['The independent checklist contains relevant supported evidence; answer its items.']
                          if coverage_review.items else [])
                if not errors:
                    metadata(answer_repair_attempts=attempt,
                             coverage_requirement_count=len(coverage_review.items),
                             coverage_verified=bool(coverage_review.review_complete))
                    break
            except ValueError as error:
                errors = [str(error)]
            if attempt:
                raise ValueError('Answer failed manual coverage validation: ' + '; '.join(errors))
            logger.warning('Repairing an incomplete answer checklist_items=%d validation_errors=%d',
                           len(coverage_review.items), len(errors))
            if getattr(entry, 'current_progress', None) is not None:
                entry.current_progress['stage'] = 'Completing missing manual details'
    except (APIError, ServerError, TimeoutError, asyncio.TimeoutError,
            RequestsReadTimeout, httpx.ReadTimeout, DeadlineExceeded) as error:
        # do not call the model again here. it can make one failure last 60-120 seconds.
        # we already have approved evidence for a safe backup answer
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
    # The answer has already passed structural, evidence and coverage checks.

    # return an unavailable message if the answer is not supported
    if not answer.supported:
        metadata(outcome='unsupported')
        if request_step and repair:
            # A brand-new case with no validated step is unsupported, not a
            # completed procedure. Do not show Issue Resolved or repair
            # controls merely because the planner initially classified the
            # question as troubleshooting.
            has_started = bool(
                repair.get('current_step')
                or repair.get('completed_steps')
                or repair.get('presented_steps')
            )
            if has_started:
                result = no_further_step(repair)
                entry.repair_workflow = repair
                return result, True
            # Retrieval found approved manual context, but the model marked
            # its structured answer unsupported. Do not discard that context
            # and show an empty unavailable message; build the safe grounded
            # repair fallback from the retrieved manual actions and assets.
            fallback = _model_fallback_response(
                media, bundle, entry, guided, question, repair,
            )
            entry.repair_workflow = repair
            return fallback, True
        if bundle.get('chunks') or media.get('images') or media.get('links'):
            # The same recovery applies to informational requests when the
            # model declines despite approved evidence being available.
            return _model_fallback_response(
                media, bundle, entry, guided, question, repair,
            ), True
        return text_response(cfg.UNAVAILABLE, documentable=False), True

    if (not request_step and (answer.repair_step is not None or answer.remaining_repair_steps)):
        raise ValueError('A new repair step requires the current step Done confirmation')

    remaining_answers = []
    if request_step:
        if answer.repair_step is None:
            if coverage_review.items and not any(item.kind == 'action' for item in coverage_review.items):
                # Some manuals explain the condition but provide no repair
                # procedure. Preserve that complete answer without inventing
                # an action merely to fill the guided template.
                result = final_blocks(answer, bundle, media, entry, True)
                result['coverage_verified'] = True
                if repair.get('completed_steps'):
                    repair['coverage_verified'] = True
                    ending = no_further_step(repair)
                    result['blocks'].extend(ending['blocks'])
                    workflow_response(result, repair)
                    entry.repair_workflow = repair
                elif repair.get('safety'):
                    result['safety'] = repair['safety']
                return result, True
            has_started = bool(
                repair.get('current_step')
                or repair.get('completed_steps')
                or repair.get('presented_steps')
            )
            if has_started:
                result = no_further_step(repair)
                entry.repair_workflow = repair
                return result, True
            # The model may correctly retrieve evidence but omit the
            # structured repair_step. Use the approved evidence fallback
            # instead of hiding the available manual answer.
            fallback = _model_fallback_response(
                media, bundle, entry, guided, question, repair,
            )
            entry.repair_workflow = repair
            return fallback, True
        seen = {_instruction_key(item['instruction']) for item in repair['completed_steps']}
        repair['coverage_verified'] = bool(coverage_review.review_complete and answer.repair_plan_complete)
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
        # The model assigns each inspected figure to the action it supports.
        # A shared page never adds other steps' images to the visible step.
        _assign_all_step_images(planned_steps, bundle, media)
        for planned in planned_steps:
            if set(planned.asset_ids) - allowed_assets:
                raise ValueError('Model supplied an unknown repair asset ID')
        # keep the checked future steps in the private cache.
        # they are shown only when the technician presses Done
        for planned in answer.remaining_repair_steps:
            future = _step_answer(answer, planned)
            _ensure_step_image_descriptions(future, planned.asset_ids, bundle, media)
            remaining_answers.append(future)
        answer = _step_answer(answer, answer.repair_step)
        _ensure_step_image_descriptions(answer, answer.repair_step.asset_ids, bundle, media)
        _apply_repair_step(answer, repair)

    # Keep only media that was really prepared for this request.
    # this also ignores any asset that failed to load
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
    # For a complete informational answer, every approved relevant asset must
    # reach the UI even if the model omitted one from relevant_asset_ids. The
    # retrieval layer has already scoped these assets to the question. Guided
    # repairs intentionally keep per-step media assignments instead.
    if not request_step and not guided:
        answer.relevant_asset_ids = list(dict.fromkeys(
            answer.relevant_asset_ids
            + [item['id'] for item in media['images']]
            + [item['id'] for item in media['links']]
        ))
    answer.blocks = [
        block for block in answer.blocks
        if block.type != "image" or block.asset_id in supplied_image_ids
    ]

    # The model is required to describe images, but a partially completed
    # structured answer can omit a description even when it selected a valid
    # image. Fill those descriptions from the already-approved manual context
    # before response_builder validates the answer. This prevents a useful
    # grounded answer from degrading to the generic fallback.
    visible_image_ids = list(dict.fromkeys(
        [identifier for identifier in answer.relevant_asset_ids
         if identifier in supplied_image_ids]
        + [block.asset_id for block in answer.blocks
           if block.type == 'image' and block.asset_id in supplied_image_ids]
    ))
    _ensure_step_image_descriptions(answer, visible_image_ids, bundle, media)

    # check that the session still exists
    await store.get(entry.id)

    # build the answer for the frontend
    with span('response_build'):
        result = final_blocks(answer, bundle, media, entry, True)
        result['coverage_verified'] = bool(coverage_review.review_complete and not coverage_review_failed)
        # test the future steps now so Done does not fail later
        # because of a missing description, fake reference or repeated step
        prepared_results = {}
        for future in remaining_answers:
            preview = future.model_copy(deep=True)
            preview_repair = deepcopy(repair)
            _apply_repair_step(preview, preview_repair, check_observations=False)
            prepared_results[_instruction_key(preview.repair_step.instruction)] = final_blocks(
                preview, bundle, media, entry, True)
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
            # Save only media used by the validated procedure. The discarded
            # candidates have already been inspected and are not needed on Done.
            # Persisting every candidate image made reloads transfer tens of MB.
            used_assets = {identifier for planned in planned_steps for identifier in planned.asset_ids}
            cached_media = {**media,
                            'images': [item for item in media['images'] if item['id'] in used_assets],
                            'links': [item for item in media['links'] if item['id'] in used_assets]}
            used_ids = {identifier for planned in planned_steps for identifier in planned.evidence_ids}
            used_ids.update(identifier for planned in planned_steps for block in planned.supporting_blocks
                            for identifier in block.evidence_ids)
            used_ids.update(item['chunk_id'] for item in cached_media['images'] + cached_media['links'])
            cached_bundle = {**bundle, 'chunks': [row for row in bundle['chunks'] if row['chunk_id'] in used_ids]}
            entry.repair_cache = {'bundle': cached_bundle, 'media': cached_media,
                                 'workflow_id': repair['workflow_id'],
                                 'plan_complete': bool(answer.repair_plan_complete and not coverage_review_failed),
                                 'plan_ready': True,
                                 'remaining_answers': remaining_answers,
                                 'prepared_results': prepared_results,
                                 'coverage_requirement_count': len(coverage_review.items),
                                 'coverage_version': 1,
                                 'wants_sources': True,
                                 'images_assigned_to_steps': True}
            metadata(repair_plan_step_count=1 + len(remaining_answers))

    # add warnings if some images or links could not be loaded
    result["asset_warnings"] = media["warnings"]

    return result, True
