"""Thought bookkeeping derived from durable voice-input receipts, not playback."""
from __future__ import annotations

import json


PENDING = "Spoken request to Ramble; response pending."
FAILED = "Spoken request to Ramble; response failed, so it may not be done."
ROUTED = "Spoken request queued for its work session; completion is not yet confirmed."
PREPARED = "Response prepared by Ramble; see speech delivery status."
SUMMARIES = {
    PENDING, FAILED, ROUTED, PREPARED,
    "Addressed requests are owned by the interactive path.",
    "Spoken request to Ramble, answered directly.",
    "The interactive response failed; completion of the addressed request is unconfirmed.",
    "Spoken request to Ramble; delivery failed, so it may not be done.",
    "Spoken request handled by Ramble's voice path; see delivery status.",
    "Sent to the existing work session.",
}


def decision(conn, thought_id):
    rows = conn.execute(
        "SELECT c.status, c.session_id FROM segments s "
        "LEFT JOIN session_claims c ON c.segment_id=s.id WHERE s.thought_id=?",
        (thought_id,),
    ).fetchall()
    # Mixed thoughts still need ordinary triage; candidates are not admitted yet.
    if not rows or any(r['status'] is None or r['status'] == 'candidate' for r in rows):
        return None
    if any(r['status'] == 'failed' for r in rows):
        summary = FAILED
        detail = "The request is retained. No automatic retry; check any unconfirmed action before trying again."
    elif any(r['status'] != 'handled' for r in rows):
        summary = PENDING
        detail = "The voice path owns this request; a result has not been confirmed."
    elif any(r['session_id'] for r in rows):
        summary = ROUTED
        detail = "Follow the work session for execution and results."
    else:
        summary = PREPARED
        detail = "Response generation finished; speech delivery and action receipts are recorded separately."
    return {'classification': 'log', 'summary': summary, 'notify': None,
            'items': [{'kind': 'note', 'text': detail, 'action': 'log',
                       'question': None, 'dispatch': None}]}


def refresh(conn, thought_ids=None):
    """Repair only generated ownership logs; never rerun triage or a side effect.

    Run after input outcomes and at startup to repair older premature success
    labels. The same fields serve CLI and Companion, including its live updates.
    """
    if thought_ids is None:
        thought_ids = [r[0] for r in conn.execute(
            "SELECT id FROM thoughts WHERE status='done' AND classification='log'"
        )]
    for tid in set(thought_ids):
        row = conn.execute("SELECT * FROM thoughts WHERE id=?", (tid,)).fetchone()
        if not row or row['status'] != 'done' or row['classification'] != 'log' or row['summary'] not in SUMMARIES:
            continue
        if conn.execute("SELECT 1 FROM dispatches WHERE thought_id=? LIMIT 1", (tid,)).fetchone():
            continue
        updated = decision(conn, tid)
        if updated is None:
            continue
        encoded = json.dumps(updated)
        if row['summary'] != updated['summary'] or row['triage_json'] != encoded:
            conn.execute("UPDATE thoughts SET summary=?,triage_json=? WHERE id=?",
                         (updated['summary'], encoded, tid))
