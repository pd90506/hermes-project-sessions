"""Bounded, optional worker card trailer. All fields remain untrusted worker data."""
import json
from .registry import safe_text

OPEN, CLOSE = '<workstream-card>', '</workstream-card>'
CONTEXT_LIMIT, ITEM_LIMIT, ITEM_COUNT = 1500, 300, 10

CARD_INSTRUCTION = '''At the end of your final answer append this structured update, with JSON
inside the delimiters (no credentials). Summarize the cumulative context you hold,
this task's key conclusions, and artifact paths; do not claim independent verification.
<workstream-card>{"context":"...","conclusions":["..."],"artifacts":["..."]}</workstream-card>
'''


def bounded(value, limit):
    # Credential screening (safe_text) runs on the full value before truncation.
    return value if len(value) <= limit else value[:limit - 1] + '…'


def parse_card(text, state, artifact=None):
    fallback = {'context': 'Structured update unavailable; consult task result and session history.',
                'conclusions': [text[:500]] if text else [], 'artifacts': [artifact] if artifact else [],
                'state': state, 'structured': False}
    # Linear scan: regex backtracking over a 32k worker result was quadratic (seconds).
    matches, end = [], 0
    while (start := (text or '').find(OPEN, end)) >= 0:
        close = text.find(CLOSE, start + len(OPEN))
        if close < 0:
            break
        matches.append(text[start + len(OPEN):close])
        end = close + len(CLOSE)
    for raw in reversed(matches):
        try:
            # Models sometimes wrap the requested JSON in a Markdown code fence.
            raw = raw.strip()
            if raw.startswith('```'):
                raw = raw[3:]
                if raw[:4].lower() == 'json':
                    raw = raw[4:]
            if raw.endswith('```'):
                raw = raw[:-3]
            data = json.loads(raw)
            # Every card is injected into, and persisted by, the main conversation:
            # truncate to a fixed total budget (~7.5k chars) instead of trusting worker sizes.
            context = bounded(safe_text(data['context']), CONTEXT_LIMIT)
            lists = {}
            for key in ('conclusions', 'artifacts'):
                values = data[key]
                if not isinstance(values, list):
                    raise ValueError('Invalid card list')
                lists[key] = [bounded(safe_text(v), ITEM_LIMIT) for v in values[:ITEM_COUNT]]
            return {'context': context, **lists, 'state': state, 'structured': True}
        except (ValueError, TypeError, KeyError, RecursionError):
            # RecursionError: deeply nested worker JSON must not abort finish().
            continue
    return fallback
