"""Independent bounded safety task; suggestions never confirm physical isolation."""
import asyncio
import json
import re
from datetime import datetime, timezone

from google.genai import types
from pydantic import BaseModel, Field

from app import config as cfg
from app.agent.planner import model_client
from app.observability.timing import span, increment, usage_metadata


class SafetySelection(BaseModel):
    relevant: bool
    precaution_ids: list[str] = Field(default_factory=list)


# Reviewed general prerequisites. Gemini selects these rather than inventing
# machine-specific isolation methods or PPE. Exact manual precautions are
# added by the main evidence-grounded answer once retrieval completes.
PRECAUTIONS = {
    'isolation': "Before physical inspection or repair, a trained, authorized person must shut down, isolate and verify all relevant energy sources using the site and equipment procedure. An emergency stop alone does not prove isolation.",
    'moving': "Keep clear of moving parts. Wait for complete stoppage before access; do not remove guards while equipment is operating.",
    'pressure': "Hydraulic or pneumatic systems can retain pressure after shutdown. Follow the approved isolation and stored-pressure procedure before opening a line or accessing a pressurized component.",
    'heat': "Allow hot surfaces to cool as required by the approved procedure before contact.",
    'electrical': "Electrical inspections and supply changes require an authorized electrical technician and the approved isolation procedure.",
    'ppe': "Use task-appropriate PPE specified by the manual and site assessment. Do not wear gloves near moving or rotating machinery.",
}


def repair_intent(question):
    if re.search(r'\b(in general|what is|what are|definition|specifications)\b', question.casefold()) and not re.search(r'\b(fix|repair|my|our|not working)\b', question.casefold()):
        return False
    return bool(re.search(r'\b(fix|repair|troubleshoot|fault|not working|no (?:hydraulic )?pressure|vibrat\w*|overheat\w*|problem|issue)\b', question.casefold()))


def fallback(question):
    ids = ['isolation', 'moving', 'ppe']
    text = question.casefold()
    if re.search(r'hydraulic|pneumatic|pressure|pump|accumulator', text): ids.insert(1, 'pressure')
    if re.search(r'electric|phase|voltage|power|motor', text): ids.insert(1, 'electrical')
    if re.search(r'hot|heat|temperature|burn', text): ids.insert(1, 'heat')
    return {'title': 'Safety prerequisites', 'bullets': [PRECAUTIONS[key] for key in ids],
            'origin': 'reviewed_general_precautions', 'confirmed': False,
            'prepared_at': datetime.now(timezone.utc).isoformat()}


def select_safety(question):
    with span('safety_generation', model=cfg.FINAL_LITE_MODEL) as measurements:
        increment('model_calls')
        response = model_client().models.generate_content(
            model=cfg.FINAL_LITE_MODEL,
            contents=json.dumps({'request': question, 'approved_precautions': PRECAUTIONS}),
            config=types.GenerateContentConfig(
                system_instruction='Select applicable prerequisite IDs for this maintenance request. No repair actions. Select only supplied IDs. Safety display does not confirm isolation. Return JSON.',
                response_mime_type='application/json', response_schema=SafetySelection,
                temperature=0, max_output_tokens=200,
                thinking_config=types.ThinkingConfig(thinking_level=types.ThinkingLevel.MINIMAL),
                http_options=types.HttpOptions(timeout=3000, retry_options=types.HttpRetryOptions(attempts=1))),
        )
        measurements.update(usage_metadata(getattr(response, 'usage_metadata', None)))
        return SafetySelection.model_validate_json(response.text)


async def _prepare_safety(entry, question):
    result = fallback(question)
    try:
        selected = await asyncio.wait_for(asyncio.to_thread(select_safety, question), 3.5)
        extra = [PRECAUTIONS[key] for key in selected.precaution_ids if key in PRECAUTIONS]
        result['bullets'] = list(dict.fromkeys(result['bullets'] + extra))
    except Exception:
        pass  # Reviewed fallback is available even during a provider outage.
    progress = getattr(entry, 'current_progress', None)
    if progress is not None:
        progress['safety'] = result
        progress['safety_status'] = 'ready'
    return result


async def prepare_safety(entry, question):
    with span('safety_ready'):
        return await _prepare_safety(entry, question)


def start_safety(entry, question):
    if not getattr(entry, 'safety_task', None):
        if getattr(entry, 'current_progress', None) is not None:
            entry.current_progress['safety_status'] = 'checking'
        entry.safety_task = asyncio.create_task(prepare_safety(entry, question))
    return entry.safety_task
