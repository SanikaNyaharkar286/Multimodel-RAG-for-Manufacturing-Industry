from app.responses.schemas import AnswerBlock
from app.sessions.session_store import store


def text_response(content):
    return {
        "blocks": [
            {
                "type": "text",
                "content": content,
            }
        ]
    }


def final_blocks(
    answer,
    bundle,
    media,
    entry,
    wants_sources,
):
    evidence = {
        row["chunk_id"]: row
        for row in bundle["chunks"]
    }

    images = {
        item["id"]: item
        for item in media["images"]
    }

    links = {
        item["id"]: item
        for item in media["links"]
    }

    descriptions = {}
    for item in answer.image_descriptions:
        if item.asset_id not in images:
            raise ValueError("Model described an image that was not supplied")
        descriptions[item.asset_id] = item.description.strip()

    blocks = []
    used_evidence = set()

    # Preserve inline placement and include relevant media omitted during formatting.
    answer_blocks = list(answer.blocks)
    def source_page(source):
        return (source["document_name"].lower().removesuffix(".pdf"),
                source["page_number"])

    answered_pages = {
        source_page(evidence[identifier])
        for block in answer_blocks for identifier in block.evidence_ids
        if identifier in evidence
    }
    shown_assets = {block.asset_id for block in answer_blocks if block.asset_id}
    for identifier in dict.fromkeys(answer.relevant_asset_ids):
        if identifier not in images and identifier not in links:
            raise ValueError("Model selected an unapproved media reference")
        if identifier not in shown_assets:
            answer_blocks.append(AnswerBlock(
                type="image" if identifier in images else "link",
                asset_id=identifier,
            ))
            shown_assets.add(identifier)

    for identifier, link in links.items():
        source = evidence[link["chunk_id"]]
        if (link["is_video"] and identifier not in shown_assets
                and source_page(source) in answered_pages):
            answer_blocks.append(AnswerBlock(
                type="link", asset_id=identifier, title="Video reference from the manual",
            ))
            shown_assets.add(identifier)

    for block in answer_blocks:
        unknown_ids = (
            set(block.evidence_ids)
            - evidence.keys()
        )

        if unknown_ids:
            raise ValueError(
                "Model supplied an unknown evidence ID"
            )

        used_evidence.update(block.evidence_ids)

        if block.type == "text" and block.content:
            if (
                answer.supported
                and not block.evidence_ids
            ):
                raise ValueError(
                    "Model omitted support for its answer"
                )

            blocks.append(
                {
                    "type": "text",
                    "content": block.content,
                }
            )

        elif block.type == "table":
            invalid_rows = any(
                len(row) != len(block.columns)
                for row in block.rows
            )

            if (
                not block.evidence_ids
                or invalid_rows
            ):
                raise ValueError(
                    "Invalid grounded table"
                )

            blocks.append(
                {
                    "type": "table",
                    "title": block.title,
                    "columns": block.columns,
                    "rows": block.rows,
                }
            )

        elif block.type == "image":
            asset = images.get(block.asset_id)

            if not asset:
                raise ValueError(
                    "Model selected an image "
                    "that was not supplied"
                )

            description = descriptions.get(block.asset_id)
            if not description:
                raise ValueError("Model omitted the image description")

            used_evidence.add(asset["chunk_id"])

            asset_id = store.put_image(
                entry,
                asset["bytes"],
            )

            blocks.append(
                {
                    "type": "image",
                    "src": f"/images/{asset_id}",
                    "caption": (
                        block.caption
                        or "Original diagram from "
                        "the approved manual."
                    ),
                    "description": description,
                }
            )

        elif block.type == "link":
            link = links.get(block.asset_id)

            if not link:
                raise ValueError(
                    "Model selected an unapproved link"
                )

            used_evidence.add(link["chunk_id"])

            default_title = (
                "Watch related video"
                if link["is_video"]
                else "Open manual reference"
            )

            blocks.append(
                {
                    "type": "link",
                    "url": link["url"],
                    "isVideo": link["is_video"],
                    "title": block.title or default_title,
                    "origin": link["origin"],
                }
            )

    if wants_sources and used_evidence:
        source_lines = []

        for identifier in sorted(used_evidence):
            row = evidence[identifier]

            sections = ", ".join(
                section["title"]
                for section in row.get("sections", [])
            )

            section_text = (
                f"; candidate section: {sections}"
                if sections
                else ""
            )

            source_lines.append(
                f"- {row['document_name']}, "
                f"PDF page {row['page_number']}"
                f"{section_text}; "
                f"chunk `{identifier}`"
            )

        blocks.append(
            {
                "type": "text",
                "content": (
                    "### Sources\n"
                    + "\n".join(source_lines)
                ),
            }
        )

    if not blocks:
        raise ValueError(
            "Model returned an empty answer"
        )

    return {"blocks": blocks}
