from typing import Literal

from pydantic import BaseModel, Field


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


class AnswerBlock(BaseModel):
    type: Literal["text", "image", "table", "link"]

    content: str = ""
    title: str = ""

    asset_id: str = Field(
        default="",
        description=(
            "An allowed image or link ID, never a URL"
        ),
    )

    caption: str = Field(default="", description="Short title identifying the image")

    columns: list[str] = Field(default_factory=list)
    rows: list[list[str]] = Field(default_factory=list)

    evidence_ids: list[str] = Field(default_factory=list)


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


class AgentAnswer(BaseModel):
    blocks: list[AnswerBlock]
    image_descriptions: list[ImageDescription] = Field(
        description="A detailed description for every image included or marked relevant"
    )

    relevant_asset_ids: list[str] = Field(
        description=(
            "All supplied image and link IDs relevant to this answer, including "
            "every useful diagram and available related video. Exclude unrelated assets."
        )
    )

    supported: bool = Field(
        description=(
            "True only when the current approved "
            "evidence answers the question"
        )
    )

    finished: bool = Field(
        description=(
            "The main guided flow is complete or "
            "the technician reports resolution"
        )
    )
