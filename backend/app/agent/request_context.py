# Determines whether a technician's question is related to the current repair case or is a new issue.
import re


# cleans the text for comparing: lowercase, no punctuation, single spaces
def canonical(text):
    return ' '.join(re.findall(r'\w+', str(text).casefold()))


# gives all the questions the user asked in one repair case
def case_questions(history, workflow_id):
    return [message.get('text', '') for message in history
            if message.get('role') == 'user' and message.get('case_id') == workflow_id]


# checks if the question is the same as the case's issue or one of its earlier questions
def repeated_case_question(question, issue, original_questions):
    normalized = canonical(question)
    return bool(normalized and normalized in {canonical(value) for value in [issue, *original_questions]})


# true only if the user clearly says it is a new or different issue, like 'another problem'
def clearly_new_topic(question, issue):
    """Identify a changed technical subject without requiring 'new issue'.

    Gemini can conservatively mark unrelated questions as follow-ups.  A
    question about a different component/symptom must therefore start a new
    case even when the user does not say "new problem" explicitly.
    """
    text = canonical(question)
    current = canonical(issue)
    if not text:
        return False
    if current and current in text:
        return False
    if re.search(r'\b(?:new|different|another|unrelated)\s+(?:issue|problem|fault|machine|topic)\b', text):
        return True

    stop = {
        'the', 'and', 'for', 'with', 'from', 'show', 'image', 'diagram',
        'what', 'how', 'why', 'please', 'tell', 'explain', 'about', 'this',
        'that', 'machine', 'issue', 'problem', 'fix', 'inspect', 'should',
        'can', 'does', 'is', 'are', 'my', 'me', 'a', 'an', 'to', 'of', 'in',
    }
    words = lambda value: {w for w in canonical(value).split()
                           if len(w) >= 4 and w not in stop}
    question_words = words(text)
    issue_words = words(current)
    if not question_words or not issue_words:
        return False

    # Pronoun/continuation language should remain attached to the active case.
    if re.search(r'\b(?:still|next|continue|done|after|before|same|above|previous|current|it|that|this)\b', text):
        return False

    # A substantive question with no technical vocabulary in common is a new
    # subject (for example, control-panel/oil-pump versus blade vibration).
    return not (question_words & issue_words)


# a safe backup check used when the planner is not working.
# it guesses if the question is a follow-up to the current repair, using the wording
# (like 'still', 'next', 'done', 'why that') and words shared with the issue or step
def fallback_follow_up(question, state):
    """a careful check for follow-up questions if the planner is not available."""
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
