# this file checks and formats the agent's answer for the frontend

import hashlib
from app.responses.schemas import AnswerBlock
from app.sessions.session_store import store


# creates a simple text-only reply
def text_response(content, documentable=True):
    return {
        "documentable": documentable,
        "blocks": [
            {
                "type": "text",
                "content": content,
            }
        ]
    }


# checks the answer and builds the final response blocks
def final_blocks(
    answer,
    bundle,
    media,
    entry,
    wants_sources,
):
    # make lookup tables for evidence and supplied media
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

    # check that image descriptions refer to supplied images
    descriptions = {}
    for item in answer.image_descriptions:
        if item.asset_id not in images:
            raise ValueError("Model described an image that was not supplied")
        descriptions[item.asset_id] = item.description.strip()

    blocks = []
    shown_image_digests = set()
    used_evidence = set()

    # keep the agent's block order and add relevant media if needed
    answer_blocks = list(answer.blocks)

    # gets the document and page for a source
    def source_page(source):
        return (source["document_name"].lower().removesuffix(".pdf"),
                source["page_number"])

    # find the pages used in the answer and the media already included
    answered_pages = {
        source_page(evidence[identifier])
        for block in answer_blocks for identifier in block.evidence_ids
        if identifier in evidence
    }
    shown_assets = {block.asset_id for block in answer_blocks if block.asset_id}

    # add relevant images or links that the agent did not include in its blocks
    for identifier in dict.fromkeys(answer.relevant_asset_ids):
        if identifier not in images and identifier not in links:
            raise ValueError("Model selected an unapproved media reference")
        if identifier not in shown_assets:
            answer_blocks.append(AnswerBlock(
                type="image" if identifier in images else "link",
                asset_id=identifier,
            ))
            shown_assets.add(identifier)

    # add related videos for pages used in the answer
    for identifier, link in links.items():
        source = evidence[link["chunk_id"]]
        if (link["is_video"] and identifier not in shown_assets
                and source_page(source) in answered_pages):
            answer_blocks.append(AnswerBlock(
                type="link", asset_id=identifier, title="Video reference from the manual",
            ))
            shown_assets.add(identifier)

    # check and format each answer block
    for block in answer_blocks:
        unknown_ids = (
            set(block.evidence_ids)
            - evidence.keys()
        )

        # reject evidence ids that were not supplied
        if unknown_ids:
            raise ValueError(
                "Model supplied an unknown evidence ID"
            )

        used_evidence.update(block.evidence_ids)

        # add a text block and check its evidence
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

        # check and add a table block
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

        # check an image, save it for the session, and add it to the response
        elif block.type == "image":
            asset = images.get(block.asset_id)

            if not asset:
                raise ValueError(
                    "Model selected an image "
                    "that was not supplied"
                )
            digest = hashlib.sha256(asset['bytes']).digest()
            if digest in shown_image_digests:
                continue
            shown_image_digests.add(digest)

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
                    "source": {'document_name': asset.get('document', evidence[asset['chunk_id']]['document_name']),
                               'page_number': asset.get('page', evidence[asset['chunk_id']]['page_number'])},
                }
            )

        # check a link and add it to the response
        elif block.type == "link":
            link = links.get(block.asset_id)

            if not link:
                raise ValueError(
                    "Model selected an unapproved link"
                )

            used_evidence.add(link["chunk_id"])

            # choose a default title based on the link type
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

    # reject an answer if it has no blocks
    if not blocks:
        raise ValueError(
            "Model returned an empty answer"
        )

    # Sources are structured UI data. Keep one concise entry per manual page;
    # rendering them again as a Markdown block duplicated the same reference
    # and exposed internal section-candidate metadata to technicians.
    sources = []
    seen_sources = set()
    for key in sorted(used_evidence):
        row = evidence[key]
        identity = (row['document_name'], row['page_number'])
        if identity in seen_sources:
            continue
        seen_sources.add(identity)
        sources.append({
            'document_name': row['document_name'],
            'page_number': row['page_number'],
        })

    return {"blocks": blocks, 'sources': sources}
