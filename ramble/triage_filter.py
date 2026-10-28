"""Conservative Jev first pass: only confidently unneeded full triage is skipped."""
from __future__ import annotations

import json
import logging
import math
import time

from . import config, latency
from .jev import JevClient, JevError

log = logging.getLogger('ramble.triage_filter')
CATEGORIES = {'ambient', 'personal', 'log', 'actionable', 'uncertain'}


def probability(value):
    if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= 1:
        raise JevError('invalid_probability')
    return value


def parse_assessment(response, model):
    if not isinstance(response, dict) or response.get('model') != model:
        raise JevError('invalid_model')
    answers = response.get('answers')
    if not isinstance(answers, dict) or set(answers) != {'category', 'needs_review'}:
        raise JevError('invalid_answers')
    category, review = answers['category'], answers['needs_review']
    if not isinstance(category, dict) or category.get('type') != 'choice':
        raise JevError('invalid_category')
    probabilities = category.get('probabilities')
    if not isinstance(probabilities, dict) or set(probabilities) != CATEGORIES:
        raise JevError('invalid_categories')
    probabilities = {key: probability(value) for key, value in probabilities.items()}
    choice = category.get('choice')
    if (not isinstance(choice, str) or choice not in CATEGORIES
            or probabilities[choice] != max(probabilities.values())
            or not math.isclose(sum(probabilities.values()), 1, abs_tol=.02)):
        raise JevError('invalid_distribution')
    if not isinstance(review, dict) or review.get('type') != 'noul':
        raise JevError('invalid_review')
    return {'category': choice, 'probability': probabilities[choice],
            'confidence': probability(category.get('confidence')),
            'needs_review': probability(review.get('noul')), 'response': response}


class TriageFilter:
    def __init__(self, cfg):
        self.cfg = cfg
        # Separate from the fast lane, so background work cannot occupy its slot.
        self.jev = JevClient(cfg)

    def decide(self, prompt, thought_id, *, recent_questions=False):
        """Return a passive decision or None for the unchanged full triage path."""
        start = time.monotonic()
        decision = None
        audit = {'backend': 'jev', 'model': self.cfg.get('jev_model', 'jev-1.13.0'),
                 'input': prompt, 'route': 'full_triage', 'reason': 'disabled'}
        try:
            if not self.cfg.get('triage_jev_enabled', True):
                pass
            elif recent_questions:
                # Even a bare yes/no may answer one. The current journal does not
                # track resolution, so conservatively retain its entire 2h window.
                audit['reason'] = 'recent_question'
            elif not self.jev.available:
                audit['reason'] = 'unavailable'
            else:
                thresholds = {
                    'probability': probability(self.cfg.get('triage_jev_min_probability', .95)),
                    'confidence': probability(self.cfg.get('triage_jev_min_confidence', .8)),
                    'needs_review': probability(self.cfg.get('triage_jev_max_review', .1)),
                }
                if thresholds['probability'] <= .5 or thresholds['needs_review'] >= .5:
                    raise JevError('invalid_threshold')
                # No truncation: oversize thoughts or context go to full triage.
                rules = config.TRIAGE_PROMPT.read_text()
                questions = json.loads((config.REPO / 'prompts/triage-filter.json').read_text())
                audit.update(rules=rules, questions=questions, thresholds=thresholds)
                assessment = self.jev.evaluate(
                    {'triage_rules': rules, 'thought_input': prompt}, questions, parse_assessment,
                )
                audit['assessment'] = assessment
                passive = assessment['category'] in {'ambient', 'personal'}
                confident = (assessment['probability'] >= thresholds['probability']
                             and assessment['confidence'] >= thresholds['confidence']
                             and assessment['needs_review'] <= thresholds['needs_review'])
                audit['reason'] = 'useful_or_uncertain' if not passive else 'low_confidence'
                if passive and confident:
                    decision = {
                        'classification': assessment['category'],
                        'summary': 'Background speech.' if assessment['category'] == 'ambient' else 'Casual conversation.',
                        'items': [], 'notify': None,
                    }
                    audit.update(route='skip_full_triage', reason='confident_passive', decision=decision)
        except Exception as exc:
            # Never drop a thought because the optimization failed. Error codes
            # are safe; raw provider bodies and credentials never enter logs.
            audit['reason'] = str(exc) if isinstance(exc, JevError) else type(exc).__name__
            decision = None
            log.warning('Jev triage fallback: %s (%s)', thought_id, audit['reason'])
        audit['elapsed_s'] = time.monotonic() - start
        try:
            config.TRIAGE_DIR.mkdir(parents=True, exist_ok=True)
            (config.TRIAGE_DIR / f'{thought_id}.jev.json').write_text(json.dumps(audit, indent=2))
            if decision is not None:
                # Preserve the normal ramble show / triage/<id>.json debug path.
                (config.TRIAGE_DIR / f'{thought_id}.json').write_text(json.dumps({
                    **audit, 'result': json.dumps(decision),
                }, indent=2))
        except OSError:
            log.warning('Jev triage audit unavailable: %s; using full triage', thought_id)
            decision = None
            audit.update(route='full_triage', reason='audit_unavailable')
        scores = {key: audit['assessment'][key]
                  for key in ('category', 'probability', 'confidence', 'needs_review')
                  if 'assessment' in audit}
        latency.event('triage_filter', thought_id=thought_id,
                      skip_full_triage=decision is not None, reason=audit['reason'],
                      elapsed_s=audit['elapsed_s'], **scores)
        return decision
