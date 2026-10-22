# this file creates the maintenance agent and sets how it should answer

from google.adk.agents import Agent
from google.genai import types

from app.config import FINAL_LITE_MODEL, MACHINE, MODEL, MODEL_TIMEOUT_MS
from app.responses.schemas import AgentAnswer


from .instructions import AGENT_INSTRUCTION


# set up the maintenance agent
root_agent = Agent(
    name="maintenance_agent",
    model=MODEL,
    description=(
        f"Read-only maintenance assistant "
        f"for approved {MACHINE} manuals"
    ),
    instruction=AGENT_INSTRUCTION,

    # keep the full conversation in adk without sending it all to the model
    # the model gets current evidence and selected recent context
    include_contents="none",

    # set the format used for the agent's answer
    output_schema=AgentAnswer,
    output_key="last_answer",

    # set how the model generates its answer
    generate_content_config=types.GenerateContentConfig(
        temperature=0.1,
        max_output_tokens=7000,
        thinking_config=types.ThinkingConfig(
            thinking_level=types.ThinkingLevel.LOW,
        ),
        http_options=types.HttpOptions(
            # Visual repair answers can legitimately take longer while the
            # model inspects the required manual images. Keep one attempt so
            # failures do not become 90-second retry chains.
            # Keep a failed request bounded; the route has an approved-evidence
            # fallback and must not wait through a provider retry chain.
            timeout=MODEL_TIMEOUT_MS,
            retry_options=types.HttpRetryOptions(attempts=1, max_delay=1),
        ),
    ),
)


def _response_agent(model, name):
    """Build the lightweight final-answer variant with the same contract."""
    return Agent(
        name=name,
        model=model,
        description=f"Read-only maintenance assistant for approved {MACHINE} manuals",
        instruction=AGENT_INSTRUCTION,
        include_contents="none",
        output_schema=AgentAnswer,
        output_key="last_answer",
        generate_content_config=types.GenerateContentConfig(
            temperature=0.1,
            max_output_tokens=6000,
            thinking_config=types.ThinkingConfig(
                thinking_level=types.ThinkingLevel.MINIMAL,
            ),
            http_options=types.HttpOptions(
                timeout=MODEL_TIMEOUT_MS,
                retry_options=types.HttpRetryOptions(attempts=1, max_delay=1),
            ),
        ),
    )


lite_agent = _response_agent(FINAL_LITE_MODEL, "maintenance_agent_lite")
