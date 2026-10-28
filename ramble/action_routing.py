"""Shared action capabilities, conservative command recognition and durable recovery.

Recognition selects an executor, never authorizes speech or invents action arguments.
Only admitted voice input or an explicit Companion message may enter this queue.
"""
from __future__ import annotations

import copy
import json
import re
import time

from .direct_routing import request_clauses


def capabilities():
    from . import reminders, time_entries
    return {name: tuple(schema['properties']['operation']['enum']) for name, schema in (
        ('time_entry', time_entries.ACTION_SCHEMA), ('reminder', reminders.ACTION_SCHEMA))}


def guidance():
    return ('Executable Ramble actions (generated from installed adapter schemas):\n'
            + json.dumps(capabilities())
            + '\nOnly these operations are supported. A missing operation is unavailable, '
            'not a reason to delegate or ask the user to repeat the request. '
            'Use the internal action queue for an already accepted request; '
            'ask only for missing arguments or authorization. '
            'Message acknowledgement and an idle conversation are not action completion.')


def intent(text):
    """Recognize command-shaped controls, excluding quotes and engineering requests."""
    for clause in request_clauses(text):
        clause = clause.lower()
        # An embedded imperative in an explanation is not a new action.
        if re.match(r'(?:build|fix|implement|debug|test|explain|investigate|add support|show me how)\b', clause):
            continue
        timer = re.search(r'\b(?:toggl|toggle|timers?|time[ -]entr(?:y|ies)|time tracking)\b', clause)
        reminder = re.search(r'\b(?:reminders?|check[ -]ins?)\b', clause)
        if re.match(r'remind me\b', clause):
            return ('reminder', 'create')
        if not timer and not reminder:
            if re.match(r'(?:what am i tracking|start tracking|stop tracking)\b', clause):
                timer = True
            else:
                continue
        family = 'time_entry' if timer else 'reminder'
        verb = re.match(r'(start|stop|create|add|set|rename|change|update|edit|put|move|reschedule|cancel|delete|complete|mark|backdate|show|list|read|what|which|am|is|do)\b', clause)
        if not verb:
            continue
        verb = verb[1]
        if family == 'time_entry':
            operation = ('start' if verb in {'start', 'create', 'add'} else
                         'stop' if verb in {'stop', 'cancel'} else
                         'current' if verb in {'show', 'list', 'read', 'what', 'which', 'am', 'is', 'do'} else
                         verb if verb in {'delete', 'backdate'} else 'update')
            if re.search(r'\b(?:completed|yesterday|backdat\w*)\b', clause) and operation == 'update':
                operation = 'edit_completed'
        else:
            operation = ('create' if verb in {'create', 'add', 'set', 'start'} else
                         'list' if verb in {'show', 'list', 'read', 'what', 'which', 'am', 'is', 'do'} else
                         'cancel' if verb in {'cancel', 'delete', 'stop'} else
                         'complete' if verb in {'complete', 'mark'} else 'reschedule')
        return family, operation
    return None


def supported(target):
    return target[1] in capabilities().get(target[0], ())


def unavailable(target):
    operation = target[1].replace('_', ' ')
    name = 'Toggl time entries' if target[0] == 'time_entry' else 'reminders'
    return f'I cannot {operation} {name} with the installed controls. No action was taken.'


def reply_schema(base, target):
    """An action request cannot escape into generic worker dispatch."""
    schema = copy.deepcopy(base)
    props = schema['properties']
    props['route'] = {'type': 'null'}
    props['handoff'] = {'type': 'boolean', 'enum': [False]}
    props['reminder' if target[0] == 'time_entry' else 'time_entry'] = {'type': 'null'}
    props[target[0]]['properties']['operation']['enum'] = [target[1]]
    props['resolution'] = {'type': 'string', 'enum': ['action', 'needs_input', 'failed']}
    schema['required'].append('resolution')
    return schema


def validate_answer(answer, target):
    action = answer.get(target[0])
    other = answer.get('reminder' if target[0] == 'time_entry' else 'time_entry')
    if other is not None:
        return False
    if action is not None:
        valid = (isinstance(action, dict) and action.get('operation') == target[1]
                 and answer.get('resolution', 'action') == 'action')
        if valid:
            answer.update(route=None, handoff=False)
        return valid
    if answer.get('route') or answer.get('handoff'):
        return False
    if answer.get('resolution') == 'failed':
        return bool(answer.get('reply'))
    reply = answer.get('reply', '')
    return (answer.get('resolution') == 'needs_input' and '?' in reply
            and not re.search(r'\b(?:ask|address|tell) ramble\b|\bfast[ -]lane\b|\brepeat (?:the |your |that )?request\b', reply, re.I))


def enqueue(conn, items, *, session_id=None, epoch=None, target=None):
    from .reminders import voice_key
    request_id = voice_key(items)
    text = ' '.join(i['text'] for i in items)
    target = target or intent(text)
    if target is None:
        raise ValueError('This request is not a recognized action.')
    now = time.time()
    conn.execute('INSERT OR IGNORE INTO action_requests '
                 '(id,session_id,epoch,items,text,family,operation,status,created_at,updated_at) '
                 "VALUES (?,?,?,?,?,?,?,'pending',?,?)",
                 (request_id, session_id, epoch, json.dumps(items), text, *target, now, now))
    saved = conn.execute('SELECT * FROM action_requests WHERE id=?', (request_id,)).fetchone()
    if saved['text'] != text:
        raise ValueError('This request ID already belongs to another action.')
    return dict(saved)


def finish(conn, request_id, status, reply):
    conn.execute('UPDATE action_requests SET status=?,reply=?,updated_at=? WHERE id=?',
                 (status, reply, time.time(), request_id))


def recover_interrupted(conn):
    # A process may have died after an external write. Never replay it.
    conn.execute("UPDATE action_requests SET status='unconfirmed',reply=?,updated_at=? "
                 "WHERE status='running'",
                 ('Ramble restarted before confirming this action. Check its current state before trying again.', time.time()))


def pending(conn):
    row = conn.execute("SELECT * FROM action_requests WHERE status='pending' ORDER BY created_at LIMIT 1").fetchone()
    return dict(row) if row else None


def current(conn, session_id=None):
    sql = 'SELECT id,session_id,text,family,operation,status,reply,created_at,updated_at FROM action_requests'
    args = ()
    if session_id:
        sql += ' WHERE session_id=?'
        args = (session_id,)
    row = conn.execute(sql + ' ORDER BY created_at DESC,rowid DESC LIMIT 1', args).fetchone()
    return dict(row) if row else None
