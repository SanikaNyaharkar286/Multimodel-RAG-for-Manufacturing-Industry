# this file searches the manuals and uses the agent to answer chat questions


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
from .conversation_context import latest_user_photo, recent_context


# connects the agent to the session service
runner = Runner(agent=root_agent, app_name=cfg.APP_NAME, session_service=session_service)


# plans the search and gets an answer for the user's question
async def answer_question(entry, question, photo):
    # get the saved details for this session
    state = await store.state(entry)

    # check if the user is asking which manuals are available
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

    # list the available manuals without running the agent
    if inventory_request:
        documents = await asyncio.to_thread(list_available_documents)
        if not documents:
            return text_response(cfg.UNAVAILABLE), False

        # group document names by machine
        grouped = {}
        for row in documents:
            grouped.setdefault(row["machine_name"], []).append(
                row["document_name"]
            )

        # prepare the list of manuals to return
        lines = ["The following machine documents are available:"]
        for machine, names in grouped.items():
            lines.append(f"\n**{machine}**")
            lines.extend(f"- {name}" for name in names)

        return text_response("\n".join(lines)), False

    # understand what the user is asking
    previous_photo = latest_user_photo(entry.history) if photo is None else None
    plan = await asyncio.to_thread(
        understand,
        question,
        state,
        entry.history,
        photo,
        previous_photo,
    )

    # Only carry the earlier upload into generation when the planner selected it.
    using_previous_photo = photo is None and plan.use_previous_photo and previous_photo is not None
    if using_previous_photo:
        photo = previous_photo

    # reply if the requested machine is not in the manuals
    if plan.model_mismatch:
        response = text_response(
            "- The requested machine is not present in the indexed manuals.\n"
            "- Please confirm the machine model or provide its matching manual."
        )

        return response, False

    # ask the user for more details if needed
    if plan.clarify:
        return text_response(plan.clarify), False

    # reply if the question is outside the app's scope
    if plan.out_of_scope:
        return text_response(cfg.UNAVAILABLE), False

    # keep the same topic for a follow-up question
    if plan.follow_up and not using_previous_photo:
        topic = state.get("main_topic", "")
    else:
        topic = plan.topic

    # choose a topic or search query
    topic = topic or plan.topic or plan.query

    # keep guided steps active during follow-up questions
    guided = (
        plan.guided
        or (
            plan.follow_up
            and state.get("guided", False)
        )
    )

    # get the previous step number for a follow-up
    current_step = (
        state.get("step_number", 0)
        if plan.follow_up
        else 0
    )

    # work out which guided step to show
    if guided:
        increment = 1 if plan.advance else 0
        step = max(1, current_step + increment)
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
    bundle = await asyncio.to_thread(
        search_manual,
        query_text,
        plan.wants_visual,
        plan.wants_video,
    )

    # reply if no matching manual information was found
    if not bundle["chunks"]:
        return text_response(cfg.UNAVAILABLE), False

    # prepare related images and links
    media = await asyncio.to_thread(
        prepare_media,
        bundle,
        plan.wants_visual,
        plan.wants_video,
    )

    # collect the current topic and answer settings
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

    # prepare recent messages to give the agent context
    recent = recent_context(entry.history, text_limit=1800)

    # give the evidence short ids for this request
    evidence_ids = {
        row["chunk_id"]: f"evidence-{index + 1}"
        for index, row in enumerate(bundle["chunks"])
    }
    source_ids = {value: key for key, value in evidence_ids.items()}

    # add the short evidence ids to the image details
    image_information = [
        {
            **{key: value for key, value in image.items() if key != "bytes"},
            "chunk_id": evidence_ids[image["chunk_id"]],
        }
        for image in media["images"]
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
            for row in bundle["chunks"]
        ],
        "allowed_images": image_information,
        "allowed_links": [
            {**link, "chunk_id": evidence_ids[link["chunk_id"]]}
            for link in media["links"]
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

    # prepare a place for the agent's final answer
    final_text = ""

    # run the agent with the question and supporting information
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
        # save the final text from the agent's response
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

    # check that the agent's answer has the expected format
    answer = AgentAnswer.model_validate_json(
        final_text
    )

    # change the short evidence ids back to the original ids
    for block in answer.blocks:
        if any(identifier not in source_ids for identifier in block.evidence_ids):
            raise ValueError("Model supplied an unknown evidence ID")
        block.evidence_ids = [source_ids[identifier] for identifier in block.evidence_ids]

    # return an unavailable message if the answer is not supported
    if not answer.supported:
        return text_response(cfg.UNAVAILABLE), True

    # check that the session still exists
    await store.get(entry.id)

    # build the answer for the frontend
    result = final_blocks(
        answer,
        bundle,
        media,
        entry,
        plan.wants_sources,
    )

    # save the topic and guided-answer progress
    await store.update_state(
        entry,
        {
            "main_topic": topic,
            "guided": guided and not answer.finished,
            "step_number": step,
            "finished": answer.finished,
        },
    )

    # add warnings if some images or links could not be loaded
    result["asset_warnings"] = media["warnings"]

    return result, True
