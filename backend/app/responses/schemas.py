# this file defines the data formats for plans and agent answers

from typing import Literal

from pydantic import BaseModel, Field


# describes how the planner understands a user's request
class QueryPlan(BaseModel):
    query: str = Field(
        description=(
            "Standalone manual search query, "
            "including component and symptom"
        )
    )

    topic: str = Field(
        description=(
            "Main troubleshooting issue. "
            "Preserve it for related follow-ups."
        )
    )

    follow_up: bool

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


# describes the full answer returned by the agent
class AgentAnswer(BaseModel):
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