import asyncio
import json
from google.adk.agents.run_config import RunConfig
from google.adk.runners import Runner
from google.genai import types
from app import config as cfg
from app.agent.agent import root_agent
from app.agent.planner import understand
from app.media.prepare_media import prepare_media
from app.responses.response_builder import text_response, final_blocks
from app.responses.schemas import AgentAnswer
from app.retrieval.manual_search import list_available_documents, search_manual
from app.sessions.session_store import session_service, store

runner = Runner(agent=root_agent, app_name=cfg.APP_NAME, session_service=session_service)


async def answer_question(entry, question, photo):
    state = await store.state(entry)

    inventory_request = (
        not photo
        and any(
            phrase in question.lower()
            for phrase in (
                "which machine documents",
                "what machine documents",
                "which manuals",
                "what manuals",
                "available machines",
                "available documents",
            )
        )
    )
    if inventory_request:
        documents = await asyncio.to_thread(list_available_documents)
        if not documents:
            return text_response(cfg.UNAVAILABLE), False
        grouped = {}
        for row in documents:
            grouped.setdefault(row["machine_name"], []).append(
                row["document_name"]
            )
        lines = ["The following machine documents are available:"]
        for machine, names in grouped.items():
            lines.append(f"\n**{machine}**")
            lines.extend(f"- {name}" for name in names)
        return text_response("\n".join(lines)), False

    plan = await asyncio.to_thread(
        understand,
        question,
        state,
        entry.history,
        photo,
    )

    if plan.model_mismatch:
        response = text_response(
            "- The requested machine is not present in the indexed manuals.\n"
            "- Please confirm the machine model or provide its matching manual."
        )

        return response, False

    if plan.clarify:
        return text_response(plan.clarify), False

    if plan.out_of_scope:
        return text_response(cfg.UNAVAILABLE), False

    if plan.follow_up:
        topic = state.get("main_topic", "")
    else:
        topic = plan.topic

    topic = topic or plan.topic or plan.query

    guided = (
        plan.guided
        or (
            plan.follow_up
            and state.get("guided", False)
        )
    )

    current_step = (
        state.get("step_number", 0)
        if plan.follow_up
        else 0
    )

    if guided:
        increment = 1 if plan.advance else 0
        step = max(1, current_step + increment)
    else:
        step = 0

    query_text = (
        plan.query
        or topic
        or question
    )

    if (
        plan.follow_up
        and topic.lower() not in query_text.lower()
    ):
        query_text = f"{topic}. {query_text}"

    bundle = await asyncio.to_thread(
        search_manual,
        query_text,
        plan.wants_visual,
        plan.wants_video,
    )

    if not bundle["chunks"]:
        return text_response(cfg.UNAVAILABLE), False

    media = await asyncio.to_thread(
        prepare_media,
        bundle,
        plan.wants_visual,
        plan.wants_video,
    )

    workflow = {
        "MAIN_TOPIC": topic,
        "GUIDED": guided,
        "STEP_NUMBER": step,
        "ADVANCE": plan.advance,
        "FOLLOW_UP": plan.follow_up,
        "CURRENT_STEP_ALREADY_GIVEN": bool(current_step),
        "wants_sources": plan.wants_sources,
        "wants_visual": plan.wants_visual,
        "wants_video": plan.wants_video,
    }

    recent = []

    for message in entry.history[-6:]:
        content = message.get("text", "")

        if message.get("response"):
            content = "\n".join(
                block.get("content", "")
                for block
                in message["response"]["blocks"]
            )

        recent.append(
            {
                "role": message["role"],
                "text": content[:1800],
            }
        )

    # Short request-local IDs prevent mistakes in long document-derived chunk IDs.
    evidence_ids = {
        row["chunk_id"]: f"evidence-{index + 1}"
        for index, row in enumerate(bundle["chunks"])
    }
    source_ids = {value: key for key, value in evidence_ids.items()}

    image_information = [
        {
            **{key: value for key, value in image.items() if key != "bytes"},
            "chunk_id": evidence_ids[image["chunk_id"]],
        }
        for image in media["images"]
    ]

    payload = {
        "question": question,
        "workflow": workflow,
        "recent_context": recent,
        "APPROVED_EVIDENCE": [
            {**row, "chunk_id": evidence_ids[row["chunk_id"]]}
            for row in bundle["chunks"]
        ],
        "allowed_images": image_information,
        "allowed_links": [
            {**link, "chunk_id": evidence_ids[link["chunk_id"]]}
            for link in media["links"]
        ],
        "asset_load_warnings": media["warnings"],
    }

    parts = [
        types.Part(
            text=json.dumps(
                payload,
                ensure_ascii=False,
            )
        )
    ]

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

    for image in media["images"]:
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

    final_text = ""

    async for event in runner.run_async(
        user_id=entry.user_id,
        session_id=entry.id,
        new_message=types.Content(
            role="user",
            parts=parts,
        ),
        run_config=RunConfig(
            max_llm_calls=2,
        ),
    ):
        if (
            event.is_final_response()
            and event.content
        ):
            final_text = "".join(
                part.text
                for part in event.content.parts or []
                if part.text
                and not part.thought
            )

    answer = AgentAnswer.model_validate_json(
        final_text
    )

    for block in answer.blocks:
        if any(identifier not in source_ids for identifier in block.evidence_ids):
            raise ValueError("Model supplied an unknown evidence ID")
        block.evidence_ids = [source_ids[identifier] for identifier in block.evidence_ids]

    if not answer.supported:
        return text_response(cfg.UNAVAILABLE), True

    await store.get(entry.id)

    result = final_blocks(
        answer,
        bundle,
        media,
        entry,
        plan.wants_sources,
    )

    await store.update_state(
        entry,
        {
            "main_topic": topic,
            "guided": guided and not answer.finished,
            "step_number": step,
            "finished": answer.finished,
        },
    )

    result["asset_warnings"] = media["warnings"]

    return result, True
