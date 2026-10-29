# this file creates the maintenance agent and sets how it should answer.
# it makes two agents: a main one for hard questions and a lite one for simple answers.

from google.adk.agents import Agent
from google.genai import types

from app.config import FINAL_LITE_MODEL, MACHINE, MODEL, MODEL_TIMEOUT_MS, MODEL_MAX_OUTPUT_TOKENS
from app.responses.schemas import AgentAnswer


from .instructions import AGENT_INSTRUCTION


# main agent used for full answers
root_agent = Agent(
    name="maintenance_agent",
    model=MODEL,
    description=(
        f"Read-only maintenance assistant "
        f"for approved {MACHINE} manuals"
    ),
    instruction=AGENT_INSTRUCTION,

    # adk keeps the full chat, but the model only gets the needed parts
    include_contents="none",

    # format of the agent's answer
    output_schema=AgentAnswer,
    output_key="last_answer",

    # how the model writes its answer
    generate_content_config=types.GenerateContentConfig(
        temperature=0.1,
        # The private plan includes the details and media for every future step.
        max_output_tokens=MODEL_MAX_OUTPUT_TOKENS,
        thinking_config=types.ThinkingConfig(
            thinking_level=types.ThinkingLevel.LOW,
        ),
        http_options=types.HttpOptions(
            # repair answers with images can take longer, so we allow more time
            # only one try, so a failed request does not retry for a long time
            # the route has a backup answer from approved evidence if this fails
            timeout=MODEL_TIMEOUT_MS,
            retry_options=types.HttpRetryOptions(attempts=1, max_delay=1),
        ),
    ),
)


def _response_agent(model, name):
    """makes a lighter and faster agent. it gives answers in the same format."""
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
            max_output_tokens=MODEL_MAX_OUTPUT_TOKENS,
            thinking_config=types.ThinkingConfig(
                thinking_level=types.ThinkingLevel.MINIMAL,
            ),
            http_options=types.HttpOptions(
                timeout=MODEL_TIMEOUT_MS,
                retry_options=types.HttpRetryOptions(attempts=1, max_delay=1),
            ),
        ),
    )


# lite agent used for simple text answers
lite_agent = _response_agent(FINAL_LITE_MODEL, "maintenance_agent_lite")
