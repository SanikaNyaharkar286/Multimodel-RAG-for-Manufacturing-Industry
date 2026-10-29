"""Machine-independent checks for one actionable instruction per step."""
import re

_VERBS = (r'inspect|check|verify|ensure|confirm|adjust|align|install|remove|replace|'
          r'clean|lubricate|calibrate|tighten|loosen|press|push|turn|open|close|'
          r'connect|disconnect|refill|fill|drain|start|stop|raise|lower|release|'
          r'clamp|unclamp|measure|test|balance|dress|select|enter|reset|switch')


def compound_instruction(text):
    # Count action clauses, not conjunctions between component names. Thus
    # "Inspect the pump and motor" is allowed, but "Inspect ... and press ..."
    # must become two steps. One action after an introductory condition is valid.
    actions = re.findall(
        rf'(?i)(?:^|[,;.]\s*|\b(?:and|then)\s+)(?:also\s+)?(?:{_VERBS})\b', text)
    return len(actions) > 1
