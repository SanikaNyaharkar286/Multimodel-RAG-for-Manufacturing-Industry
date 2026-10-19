"""Recognize common catalog questions without relying on semantic similarity."""
import re
from difflib import get_close_matches


def is_inventory_request(question):
    # Correct only catalog vocabulary, never machine identifiers or measurements.
    vocabulary = ('documents', 'document', 'manuals', 'manual', 'machines', 'machine',
                  'available', 'uploaded', 'indexed')
    words = re.findall(r"[a-z0-9-]+", question.lower())
    normalized = []
    for word in words:
        if word in ('doc', 'docs'):
            word = 'documents'
        elif len(word) >= 5 and word.isalpha():
            match = get_close_matches(word, vocabulary, n=1, cutoff=0.85)
            if match:
                word = match[0]
        normalized.append(word)
    text = ' '.join(normalized)
    subject = r'(?:machine )?(?:documents?|manuals?)|machines?'
    ending = (r'(?: (?:(?:do |can )?(?:you|u) have|you ve got|are available|'
              r'are indexed|are uploaded|available|indexed|uploaded|'
              r'in (?:your|the) (?:system|database|catalog)))?')
    return bool(re.fullmatch(
        rf'(?:please )?(?:how many|which|what|list|show|list all|show all) '
        rf'(?:the |your |all )?(?:available )?(?:{subject}){ending}(?: please)?', text
    ) or re.fullmatch(r'(?:available|indexed|uploaded) (?:machines?|documents?|manuals?)', text))


def inventory_response(documents):
    grouped = {}
    for row in documents:
        grouped.setdefault(row['machine_name'], set()).add(row['document_name'])
    count = sum(len(names) for names in grouped.values())
    if not count:
        return 'There are currently **0 indexed machine documents** available.'
    lines = [f'I have **{count} indexed documents** across **{len(grouped)} machine models**.']
    for machine, names in sorted(grouped.items()):
        lines.append(f'\n**{machine}** — {len(names)} document(s)')
        lines.extend(f'- {name}' for name in sorted(names))
    return '\n'.join(lines)
