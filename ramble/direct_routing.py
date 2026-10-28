"""Local exceptions to direct delivery; these never execute model instructions."""
import re

from .addressing import ADDRESS, NAME, has_name


def control(text):
    """Only complete, explicitly addressed controls bypass the input queue."""
    if not has_name(text):
        return None
    rest = NAME.sub(' ', ADDRESS.sub(' ', text)).strip(' ,.!?').lower()
    rest = re.sub(r'^please\s+|[,\s]+please$', '', rest).strip()
    return {
        'stop': 'stop_work', 'stop work': 'stop_work', 'stop working': 'stop_work',
        'stop talking': 'stop_talking', 'be quiet': 'stop_talking',
        'resume talking': 'resume_talking', 'start talking again': 'resume_talking',
    }.get(rest)


def request_clauses(text):
    """Yield command-shaped clauses without promoting quoted/reference text."""
    # Punctuation inside a quoted example must not create a new command.
    text = re.sub(r""""[^"\n]*"|“[^”\n]*”|(?<!\w)'[^'\n]*'(?!\w)""", ' ', text)
    for clause in re.split(r'[.!?;\n]+|,\s*(?:and then|then)\s+', text):
        clause = clause.strip(' ,')
        address = ADDRESS.match(clause) or NAME.match(clause)
        if address:
            clause = clause[address.end():]
        clause = re.sub(r'^(?:(?:okay|ok|yeah|actually|so|please|just|now|then|also|and)\b[\s,]*)+', '', clause, flags=re.I)
        clause = re.sub(r"^(?:(?:can|could|would|will) you|(?:I want|I need|I would like|I'd like) (?:you to|to)|let's)\s+", '', clause, flags=re.I)
        clause = re.sub(r'^(?:(?:please|just)\s+)+', '', clause, flags=re.I)
        yield clause.strip()


def needs_router(text):
    """Reserve routing for requested destination changes and adapter operations.

    Mentioning an agent/session, asking about earlier instructions, or constraining
    planned work belongs to the current owner. The model still resolves targets
    and arguments for actual routing requests; this helper executes nothing.
    """
    for clause in request_clauses(text):
        if re.search(
            r'^(?:switch to|talk to|start over|new chat)\b|'
            r'^(?:switch|move|send|take|put|route|transfer|hand)\b.{0,100}\b(?:to|into|in|over)\b.{0,100}\b(?:session|thread|conversation|chat|agent)\b|'
            r'^(?:start|create|open|make|launch|use)\b.{0,80}\b(?:session|thread|conversation|chat)\b|'
            r'^delegate\b|'
            r'^(?:have|ask|tell|send|launch|spawn|create|start)\s+'
            r'(?:(?:an?|the|another|new|separate|app|background|existing|two|three|four|some|more|those|these|that|my|your|\d+)\s+)*agents?\b',
            clause, re.I,
        ):
            return True
        if re.search(
            r'^(?:remind me|remember to|(?:do not|don[’\']t) let me forget)\b|'
            r'^(?:show|list|read|create|add|set|schedule|change|move|reschedule|cancel|delete|complete|mark|update)\b.{0,100}\b(?:reminders?|check[ -]ins?|time[ -]entr(?:y|ies)|toggl|timers?)\b|'
            r'^(?:what|which|when|do I|am I|is there)\b.{0,100}\b(?:reminders?|check[ -]ins?|time[ -]entr(?:y|ies)|tracking|toggl|timers?)\b|'
            r'^(?:start|stop)\b.{0,100}\b(?:tracking|timers?|toggl|time[ -]entr(?:y|ies))\b|'
            r'^(?:stop|mute|unmute|quiet|resume talking|start talking)\b|'
            r'^(?:show|publish|pull up|put)\b.{0,80}\b(?:report|brief|result)\b',
            clause, re.I,
        ):
            return True
    return False
