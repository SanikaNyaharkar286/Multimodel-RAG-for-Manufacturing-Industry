from google.adk.agents import Agent
from google.genai import types

from app.config import MACHINE, MODEL
from app.responses.schemas import AgentAnswer


from .instructions import AGENT_INSTRUCTION


root_agent = Agent(
    name="maintenance_agent",
    model=MODEL,
    description=(
        f"Read-only maintenance assistant "
        f"for approved {MACHINE} manuals"
    ),
    instruction=AGENT_INSTRUCTION,

    # ADK stores the full conversation.
    # The model receives current evidence and compact recent context,
    # avoiding repeated transmission of old manual images/passages.
    include_contents="none",

    output_schema=AgentAnswer,
    output_key="last_answer",

    generate_content_config=types.GenerateContentConfig(
        temperature=0.1,
        max_output_tokens=3500,
        thinking_config=types.ThinkingConfig(
            thinking_level=types.ThinkingLevel.LOW,
        ),
        http_options=types.HttpOptions(
            timeout=45000,
            retry_options=types.HttpRetryOptions(attempts=2, max_delay=2),
        ),
    ),
)
