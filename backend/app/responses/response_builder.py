# checks the agent's answer and turns it into blocks (text, table, image, link)
# that the frontend can show.

import re
from app.media.manual_assets import image_identity
from app.responses.schemas import AnswerBlock
from app.sessions.session_store import store


# makes a simple reply with only text
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


def _public_image_description(value):
    """Normalize image details without repeating the structured source line."""
    text = str(value or '').replace('\r', '\n').strip()
    if not text:
        return ''

    # Retrieval context is useful to the model but is not an image
    # description. The visible caption should explain the figure itself;
    # document/page source is rendered separately by the UI.
    text = re.sub(r'(?is)\b(?:manual|supporting\s+manual)\s+information\s*:\s*.*$', '', text)

    # The UI renders the authoritative document/page below the image. Remove
    # those fields from model-generated descriptions so they appear only once.
    text = re.sub(r'(?i)\b(?:document|source)\s*:\s*.*?(?=\bpage\s*:|$)', '', text)
    text = re.sub(r'(?i)\bpage\s*:\s*(?:pdf\s*)?\d+\s*', '', text)
    text = re.sub(r'(?i)\brelated\s+text\s+candidates\s*:\s*', '', text)

    # Related candidates are commonly joined with pipes in the stored
    # description. Present each candidate as its own readable bullet.
    parts = []
    for line in text.split('\n'):
        if re.match(r'(?i)^\s*[-*•]?\s*type\s*:', line):
            continue
        parts.extend(re.split(r'\s*\|\s*', line))

    bullets = []
    for part in parts:
        part = re.sub(r'^\s*[-*•]\s*', '', part).strip(' ;|')
        if not part:
            continue
        part = re.sub(r'\s{2,}', ' ', part)
        bullets.append(f'- {part}')
    return '\n'.join(dict.fromkeys(bullets))


def _add_evidence_callouts(description, asset, evidence):
    """Add numbered callout mappings found in the image's approved text."""
    row = evidence.get(asset.get('chunk_id'), {})
    context = str(row.get('content', ''))
    if not context:
        return description
    existing = description.casefold()
    additions = []
    for match in re.finditer(
        r'(?i)([a-z][^.;|\n]{2,100}?)\s*\[\s*(\d+)\s*\]', context
    ):
        subject = re.sub(r'\s+', ' ', match.group(1)).strip(' ,:-')
        subject = re.sub(r'(?i)^(?:the|a|an)\s+', '', subject)
        subject = re.sub(r'(?i)^(?:locate|identify|find)\s+', '', subject)
        number = match.group(2)
        if subject and f'callout {number}' not in existing:
            additions.append(f'- Callout {number} — {subject.capitalize()}.')
            existing += f' callout {number}'
    if not additions:
        return description
    callouts = '\n'.join(additions)
    marker = re.search(r'(?im)^\s*-\s*Technician check\s*:', description)
    if marker:
        return description[:marker.start()].rstrip() + '\n' + callouts + '\n' + description[marker.start():].lstrip()
    return '\n'.join([description, callouts])


def _readable_table_fallback(block):
    """Keep all model table values readable when a row shape is malformed.

    The model's structured table is only a presentation format. The approved
    evidence has already been checked before this point, so converting a bad
    row to text is safer than discarding the answer or returning a 502.
    """
    title = str(block.title or '').strip()
    columns = [str(value or '').strip() for value in block.columns]
    rows = [[str(value or '').strip() for value in row] for row in block.rows]
    lines = [f'### {title}' if title else '### Manual table details']
    if columns:
        lines.append(' | '.join(columns))
        lines.append(' | '.join('---' for _ in columns))
    for row in rows:
        lines.append(' | '.join(row))
    if not rows and block.content:
        lines.append(str(block.content).strip())
    return '\n'.join(line for line in lines if line).strip()


# checks the agent's answer and builds the final blocks and sources.
# it raises an error if the answer uses evidence, images or links that were not given
def final_blocks(
    answer,
    bundle,
    media,
    entry,
    wants_sources,
):
    # make lookups so we can find evidence, images and links by id
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

    # check that every image description is for an image we really gave
    descriptions = {}
    for item in answer.image_descriptions:
        if item.asset_id not in images:
            raise ValueError("Model described an image that was not supplied")
        descriptions[item.asset_id] = item.description.strip()

    blocks = []
    shown_image_digests = set()
    used_evidence = set()

    # keep the agent's block order, and add more media if needed
    answer_blocks = list(answer.blocks)

    # gives the document name and page number of a source
    def source_page(source):
        return (source["document_name"].lower().removesuffix(".pdf"),
                source["page_number"])

    # find the pages used in the answer and the media already added
    answered_pages = {
        source_page(evidence[identifier])
        for block in answer_blocks for identifier in block.evidence_ids
        if identifier in evidence
    }
    shown_assets = {block.asset_id for block in answer_blocks if block.asset_id}

    # add relevant images or links that the agent forgot to put in its blocks
    for identifier in dict.fromkeys(answer.relevant_asset_ids):
        if identifier not in images and identifier not in links:
            raise ValueError("Model selected an unapproved media reference")
        if identifier not in shown_assets:
            answer_blocks.append(AnswerBlock(
                type="image" if identifier in images else "link",
                asset_id=identifier,
            ))
            shown_assets.add(identifier)

    # add videos from the same pages that the answer used
    for identifier, link in links.items():
        source = evidence[link["chunk_id"]]
        if (answer.repair_step is None and link["is_video"] and identifier not in shown_assets
                and source_page(source) in answered_pages):
            answer_blocks.append(AnswerBlock(
                type="link", asset_id=identifier, title="Video reference from the manual",
            ))
            shown_assets.add(identifier)

    # check each block and format it for the frontend
    for block in answer_blocks:
        unknown_ids = (
            set(block.evidence_ids)
            - evidence.keys()
        )

        # reject evidence ids that were not given to the model
        if unknown_ids:
            raise ValueError(
                "Model supplied an unknown evidence ID"
            )

        used_evidence.update(block.evidence_ids)

        # text block: it must have evidence if the answer is supported
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

        # table block: it needs evidence and every row must match the columns
        elif block.type == "table":
            invalid_rows = any(
                len(row) != len(block.columns)
                for row in block.rows
            )

            if not block.evidence_ids:
                raise ValueError(
                    "Invalid grounded table"
                )

            if invalid_rows:
                # Preserve the approved table values and evidence as a text
                # block. A malformed presentation row must not discard an
                # otherwise supported answer or turn the whole request into
                # a 502 response.
                blocks.append({
                    "type": "text",
                    "content": _readable_table_fallback(block),
                })
                continue

            blocks.append(
                {
                    "type": "table",
                    "title": block.title,
                    "columns": block.columns,
                    "rows": block.rows,
                }
            )

        # image block: skip repeats, save the image in the session and add it
        elif block.type == "image":
            asset = images.get(block.asset_id)

            if not asset:
                raise ValueError(
                    "Model selected an image "
                    "that was not supplied"
                )
            digest = image_identity(asset)
            if digest in shown_image_digests:
                continue
            shown_image_digests.add(digest)

            description = _public_image_description(descriptions.get(block.asset_id))
            # Callouts must come from the inspected image description. Nearby
            # text containing [1] may refer to a different figure on the page.
            if not description:
                raise ValueError("Model omitted the image description")

            used_evidence.add(asset["chunk_id"])

            asset_id = store.put_media_image(entry, asset)

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

        # link block: it must be an approved link
        elif block.type == "link":
            link = links.get(block.asset_id)

            if not link:
                raise ValueError(
                    "Model selected an unapproved link"
                )

            used_evidence.add(link["chunk_id"])

            # pick a default title for a video or a normal link
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

    # reject the answer if nothing was added
    if not blocks:
        raise ValueError(
            "Model returned an empty answer"
        )

    # sources are sent as separate data, one entry per manual page.
    # showing them again as a text block repeated the same reference
    # and showed internal details to technicians
    sources = []
    seen_sources = set()
    source_links = {}
    for link in media.get('links', []):
        link_evidence = link.get('chunk_id')
        if link_evidence not in used_evidence:
            continue
        row = evidence.get(link_evidence)
        if not row:
            continue
        identity = (row['document_name'], row['page_number'])
        source_links.setdefault(identity, []).append({
            'url': link['url'],
            'title': 'Watch related video' if link.get('is_video')
                     else 'Open original manual reference',
            'is_video': bool(link.get('is_video')),
        })
    for key in sorted(used_evidence):
        row = evidence[key]
        identity = (row['document_name'], row['page_number'])
        if identity in seen_sources:
            continue
        seen_sources.add(identity)
        sources.append({
            'document_name': row['document_name'],
            'page_number': row['page_number'],
            'links': source_links.get(identity, []),
        })

    return {"blocks": blocks, 'sources': sources}
