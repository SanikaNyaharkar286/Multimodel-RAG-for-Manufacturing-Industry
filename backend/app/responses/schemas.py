# this file defines the data formats for plans and agent answers

from typing import Literal

from pydantic import BaseModel, Field, field_validator


# describes how the planner understands a user's request
class QueryPlan(BaseModel):
    revise_plan: bool = Field(default=False, description='New findings or image evidence contradict the current repair diagnosis and require revising the remaining plan. False for ordinary clarification questions.')
    repair: bool = Field(default=False, description='True only for a current real issue, practical repair request or repair continuation. False for full informational explanations, including during a repair.')
    intent: Literal['manual_question', 'document_inventory'] = Field(
        default='manual_question',
        description='document_inventory for requests to count/list the indexed machines or manuals; otherwise manual_question',
    )
    query: str = Field(
        description=(
            "Standalone manual search query with clear ordinary spelling, "
            "resolved follow-up references, and every requested point. "
            "Preserve negation, exact machine/part/alarm identifiers, numbers "
            "and units; do not invent missing symptoms."
        )
    )

    topic: str = Field(
        description=(
            "Main troubleshooting issue. "
            "Preserve it for related follow-ups."
        )
    )

    follow_up: bool

    use_previous_photo: bool = Field(
        default=False,
        description=(
            "The current request refers to the supplied previous user photo. "
            "Resolve image references and brief or misspelled follow-ups using "
            "conversation context; false for an unrelated new subject."
        ),
    )

    guided: bool = Field(
        description=(
            "The user wants one-step-at-a-time guidance"
        )
    )

    advance: bool = Field(
        description=(
            "Explicit request for the next main "
            "troubleshooting step"
        )
    )

    wants_visual: bool
    wants_video: bool
    wants_sources: bool

    clarify: str = Field(
        description=(
            "Short clarification question when essential "
            "input is missing; otherwise an empty string"
        )
    )

    out_of_scope: bool
    model_mismatch: bool


# describes one part of the agent's answer
class AnswerBlock(BaseModel):
    # identifies whether this block contains text, an image, a table, or a link
    type: Literal["text", "image", "table", "link"]

    content: str = ""
    title: str = ""

    # stores an allowed image or link id
    asset_id: str = Field(
        default="",
        description=(
            "An allowed image or link ID, never a URL"
        ),
    )

    # gives an image a short identifying caption
    caption: str = Field(default="", description="Short title identifying the image")

    # stores table headings and table data
    columns: list[str] = Field(default_factory=list)
    rows: list[list[str]] = Field(default_factory=list)

    # stores the manual evidence ids supporting this block
    evidence_ids: list[str] = Field(default_factory=list)
    coverage_ids: list[str] = Field(default_factory=list,
        description='IDs from the independent EVIDENCE_CHECKLIST covered by this block.')


# describes an image included with the answer
class ImageDescription(BaseModel):
    asset_id: str
    description: str = Field(
        min_length=1,
        description=(
            "Explain what this supplied image shows, its readable labels and "
            "important visible parts, and its relevance to the answer. Use clear "
            "Markdown bullets when helpful. Do not invent unreadable details."
        ),
    )


class ObservationBranch(BaseModel):
    label: str = Field(min_length=1, max_length=120)
    next_step_key: str = Field(description='Key of the next prepared step, or END. Never assume the observed result.')


class ObservationPrerequisite(BaseModel):
    step_key: str = Field(min_length=1)
    allowed_observations: list[str] = Field(min_length=1,
        description='Exact observed-result labels required before this action may be reached.')


class RepairStep(BaseModel):
    step_key: str = Field(default='', max_length=80)
    title: str = Field(default='', max_length=160)
    expected_result: str = Field(default='', max_length=1000)
    precautions: list[str] = Field(default_factory=list)
    observation_prompt: str = Field(default='', max_length=200)
    branches: list[ObservationBranch] = Field(default_factory=list)
    coverage_ids: list[str] = Field(default_factory=list,
        description='EVIDENCE_CHECKLIST action IDs performed by THIS step. Details belong in supporting_blocks.')
    requires_observations: list[ObservationPrerequisite] = Field(default_factory=list,
        description='Earlier step outcomes required for this action. Done alone never satisfies an observed-result condition.')
    instruction: str = Field(min_length=1, max_length=1200,
                             description='ONE manual-supported action as a plain paragraph. No list, numbering, later steps or independent actions.')
    evidence_ids: list[str] = Field(min_length=1)
    asset_ids: list[str] = Field(default_factory=list,
        description='Supplied image/link IDs needed for this action; include every required diagram.')
    supporting_blocks: list[AnswerBlock] = Field(default_factory=list,
        description=('Ordered grounded explanation, original table rows, images and links for THIS action. '
                     'Include all relevant conditions, values, units and limitations. Place images beside the '
                     'details they illustrate. Do not include actions belonging to another step. '
                     'Text/table blocks require evidence_ids. Media blocks require supplied asset_id.'))
    image_descriptions: list[ImageDescription] = Field(default_factory=list,
        description='Descriptions and technician checks for this step only; use the universal image format.')

    @field_validator('instruction')
    @classmethod
    def single_step(cls, value):
        import re
        value = value.strip()
        if not value or '\n' in value or re.search(r'(?i)\bstep\s*\d+|(?:^|\s)\d+[.)]\s|[•→]', value):
            raise ValueError('Return one plain repair step without a list or later steps')
        return value


class RepairAction(BaseModel):
    action: Literal['done', 'report_issue', 'resolve', 'unresolved']
    workflow_id: str = Field(min_length=1, max_length=80)
    step_id: str = Field(default='', max_length=80)
    plan_version: int = Field(default=1, ge=1)
    observation: str = Field(default='', max_length=2000)
    safety_confirmed: bool = False
    machine: str = Field(default='', max_length=120)
    issue: str = Field(default='', max_length=1000)
    cause: str = Field(default='', max_length=1000)
    action_taken: str = Field(default='', max_length=4000)
    issue_report: str = Field(default='', max_length=2000)


# describes the full answer returned by the agent
class AgentAnswer(BaseModel):
    repair_step: RepairStep | None = Field(default=None,
        description='One grounded action only when workflow.REQUEST_STEP=true. Null for explanations or no further supported action.')
    remaining_repair_steps: list[RepairStep] = Field(default_factory=list,
        description='When REQUEST_STEP=true, prepare ALL remaining distinct manual-supported actions in order. Stored privately by the server; never shown together. Empty for explanations.')
    repair_plan_complete: bool = Field(default=False,
        description='True when REQUEST_STEP=true and the current step plus remaining_repair_steps cover all supported actions in the supplied evidence. Never implies the issue is resolved.')
    # stores the text, images, tables, and links in the answer
    blocks: list[AnswerBlock]

    # gives details about each relevant image
    image_descriptions: list[ImageDescription] = Field(
        description="A detailed description for every image included or marked relevant"
    )

    # lists relevant image and link ids
    relevant_asset_ids: list[str] = Field(
        description=(
            "All supplied image and link IDs relevant to this answer, including "
            "every useful diagram and available related video. Exclude unrelated assets."
        )
    )

    # says whether the manual evidence supports the answer
    supported: bool = Field(
        description=(
            "True only when the current approved "
            "evidence answers the question"
        )
    )

    # says whether the guided troubleshooting flow is complete
    finished: bool = Field(
        description=(
            "The main guided flow is complete or "
            "the technician reports resolution"
        )
    )
