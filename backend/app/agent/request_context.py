"""Small context helpers for automatic case routing; no technical diagnosis."""
import re


def canonical(text):
    return ' '.join(re.findall(r'\w+', str(text).casefold()))


def case_questions(history, workflow_id):
    return [message.get('text', '') for message in history
            if message.get('role') == 'user' and message.get('case_id') == workflow_id]


def repeated_case_question(question, issue, original_questions):
    normalized = canonical(question)
    return bool(normalized and normalized in {canonical(value) for value in [issue, *original_questions]})


def fallback_follow_up(question, state):
    """Conservative relation check if the planner provider is unavailable."""
    active = state.get('active_repair') or {}
    topic = active.get('issue') or state.get('main_topic', '')
    if not topic:
        return False
    if repeated_case_question(question, topic, active.get('original_questions', [])):
        return True
    text = canonical(question)
    if re.search(r'\b(?:new|different|another|unrelated)\s+(?:issue|problem|fault|machine)\b', text):
        return False
    if re.search(r'^(?:still|after|continue|next|done)\b', text):
        return True
    if re.search(r'^(?:it|that|this)\s+(?:is|was|still|does|did|has|seems|means|looks|happened|one)\b', text):
        return True
    if text in ('why', 'why that', 'why this', 'what next'):
        return True
    if re.search(r'\b(?:this|that|current|previous)\s+(?:step|check|action|instruction)\b', text):
        return True
    if len(text.split()) <= 6 and re.search(r'\b(it|that|same|above|these|those)\b', text):
        return True
    ignored = {'the', 'and', 'for', 'with', 'machine', 'issue', 'problem', 'repair', 'fix',
               'show', 'image', 'diagram', 'what', 'how', 'please', 'tell', 'check', 'inspect',
               'step', 'working', 'not', 'very', 'this', 'there', 'from', 'should', 'does'}
    terms = lambda value: {word for word in canonical(value).split() if len(word) > 3 and word not in ignored}
    context = topic + ' ' + (active.get('current_step') or {}).get('instruction', '')
    return bool(terms(question) & terms(context))
