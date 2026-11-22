"""Receipt-driven source highlighting without audio, models, or a daemon."""
import copy
from pathlib import Path
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

import AppKit as A
import Foundation as F
from ramble.reading_view import ReadingService, ReadingView, utf16_span

SOURCE = 'An orbit 🌍 is a path.\n\nGravity changes its direction.  Motion continues.'
SPANS = [(0, 20), (22, 52), (54, len(SOURCE))]

def snapshot(status='paused', active=None, position=1):
    return {'controller_online': True, 'interaction': None, 'reading': {
        'id': 'article-one', 'title': 'Orbits and motion', 'source_url': None, 'text': SOURCE,
        'passages': [{'index': i, 'start': start, 'end': end, 'text': SOURCE[start:end]} for i, (start, end) in enumerate(SPANS)],
        'position': position, 'active_index': active, 'status': status, 'error': None, 'revision': 1}}


class ReadingViewTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        A.NSApplication.sharedApplication()

    def setUp(self):
        self.service = SimpleNamespace(snapshot=Mock(return_value=snapshot()), command=Mock(return_value=snapshot()))
        self.view = ReadingView(self.service)
        self.addCleanup(self.view.close)
        self.view.view.setFrame_(F.NSMakeRect(0, 0, 736, 720))
        self.view.next_poll = time.monotonic() + 60

    def complete(self):
        deadline = time.monotonic() + 2
        while self.view.pending is None and time.monotonic() < deadline:
            time.sleep(.005)
        self.assertIsNotNone(self.view.pending)
        self.view.tick()

    def test_utf16_source_offsets_and_actual_start_highlight(self):
        self.assertEqual(utf16_span('🌍 Hi', 2, 4), (3, 2))
        self.view.render(snapshot('preparing', None))
        self.assertEqual(str(self.view.source.string()), SOURCE)
        self.assertIsNone(self.view.highlight)
        self.assertFalse(self.view.preparation.isHidden())
        self.view.render(snapshot('playing', 1))
        self.assertEqual(self.view.highlight, utf16_span(SOURCE, *SPANS[1]))
        self.assertEqual(str(self.view.source.string()), SOURCE)
        self.assertEqual(self.view.play.title(), 'Pause')
        self.view.render(snapshot('paused', None))
        self.assertEqual(self.view.play.title(), 'Resume')
        self.assertIn('place saved', self.view.status.stringValue())
        attributes = self.view.source.textStorage().attributesAtIndex_effectiveRange_(self.view.highlight[0], None)[0]
        self.assertEqual(attributes[A.NSBackgroundColorAttributeName], A.NSColor.unemphasizedSelectedTextBackgroundColor())
        self.assertNotIn(A.NSUnderlineStyleAttributeName, attributes)
        self.assertIn('Saved place', self.view.source_label.stringValue())
        self.assertTrue(self.view.preparation.isHidden())

    def test_explanations_are_separate_and_bound_to_article_identity(self):
        value = snapshot()
        value['interaction'] = dict(reading_id='another-article', revision=1, status='answered', question='Old question', answer='Old answer')
        self.view.render(value)
        self.assertEqual(self.view.answer.string(), '')
        value['interaction']['reading_id'] = 'article-one'
        self.view.render(value)
        self.assertIn('Old answer', self.view.answer.string())
        self.assertEqual(self.view.source.string(), SOURCE)
        value['interaction']['status'] = 'cancelled'
        self.view.render(value)
        self.assertNotIn('Old answer', self.view.answer.string())

    def test_answer_revision_must_match_current_article_revision(self):
        value = snapshot()
        value['reading']['revision'] = 3
        value['interaction'] = dict(reading_id='article-one', revision=2, status='answered', question='Prior question', answer='Stale answer')
        self.view.render(value)
        self.assertEqual(self.view.answer.string(), '')
        self.assertEqual(self.view.source.string(), SOURCE)
        value['interaction']['status'] = 'answering'
        self.view.render(value)
        self.assertTrue(self.view.ask.isEnabled())

    def test_manual_scroll_disables_follow_until_explicitly_restored(self):
        self.view.render(snapshot('playing', 0, 0))
        self.view.actions.scrolled_(None)
        self.view.render(snapshot('playing', 2, 2))
        self.assertFalse(self.view.following)
        self.assertEqual(self.view.follow.state(), 0)
        self.view.follow.setState_(1)
        self.view.actions.follow_(self.view.follow)
        self.assertTrue(self.view.following)

    def test_preparing_feedback_is_immediate_and_failed_start_keeps_draft(self):
        release = threading.Event()
        def start(*args, **kwargs):
            release.wait(1)
            raise ValueError('Article exceeds the source limit')
        self.service.command.side_effect = start
        self.view.editor.setString_('Unchanged draft')
        self.view.actions.startArticle_(None)
        self.assertEqual(self.view.status.stringValue(), 'Preparing reading…')
        self.assertEqual(self.view.start.title(), 'Preparing…')
        release.set(); self.complete()
        self.assertEqual(self.view.editor.string(), 'Unchanged draft')
        self.assertTrue(self.view.editing)
        self.assertIn('source limit', self.view.status.stringValue())

    def test_pause_seek_and_question_keep_captured_article_identity(self):
        self.view.render(snapshot('playing', 1))
        self.view.actions.playPause_(None)
        self.complete()
        self.assertEqual(self.service.command.call_args.args, ('pause',))
        self.assertEqual(self.service.command.call_args.kwargs['reading_id'], 'article-one')
        self.view.actions.seek_(self.view.next)
        self.complete()
        self.assertEqual(self.service.command.call_args.kwargs['passage_index'], 2)
        self.view.question.setStringValue_('Why does direction change?')
        self.view.actions.ask_(None)
        self.complete()
        self.assertEqual(self.service.command.call_args.args, ('question',))
        self.assertEqual(self.view.question.stringValue(), '')

    def test_offline_keeps_source_visible_and_disables_resume_questions(self):
        value = snapshot(); value['controller_online'] = False
        self.view.render(value)
        self.assertEqual(self.view.source.string(), SOURCE)
        self.assertFalse(self.view.play.isEnabled())
        self.assertFalse(self.view.ask.isEnabled())
        self.assertTrue(self.view.previous.isEnabled())
        value['reading'].update(status='playing', active_index=1)
        self.view.render(value)
        attributes = self.view.source.textStorage().attributesAtIndex_effectiveRange_(self.view.highlight[0], None)[0]
        self.assertEqual(attributes[A.NSBackgroundColorAttributeName], A.NSColor.unemphasizedSelectedTextBackgroundColor())
        self.assertEqual(self.view.play.title(), 'Resume')
        self.assertFalse(self.view.play.isEnabled())

    def test_native_space_targets_only_readonly_source(self):
        self.view.render(snapshot('playing', 1))
        event = Mock()
        event.charactersIgnoringModifiers.return_value = ' '
        event.isARepeat.return_value = False
        self.view.source.keyDown_(event)
        self.complete()
        self.assertEqual(self.service.command.call_args.args, ('pause',))
        self.assertIsNone(getattr(self.view.editor, 'reading_owner', None))

    def test_minimum_layout_contains_controls_and_distinct_explanation(self):
        self.view.render(snapshot())
        self.view.layout()
        self.assertLessEqual(F.NSMaxX(self.view.recap.frame()), self.view.view.bounds().size.width)
        self.assertLessEqual(F.NSMaxY(self.view.source_scroll.frame()), self.view.question.frame().origin.y)
        self.assertLessEqual(F.NSMaxY(self.view.answer_scroll.frame()), self.view.view.bounds().size.height)
        bitmap = self.view.view.bitmapImageRepForCachingDisplayInRect_(self.view.view.bounds())
        self.view.view.cacheDisplayInRect_toBitmapImageRep_(self.view.view.bounds(), bitmap)
        self.assertGreater(bitmap.pixelsWide(), 0)

class ReadingServiceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='ramble-test-reading-service-', dir='/tmp')
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name)
        self.service = ReadingService(self.home)
        self.store = self.service.store()
        # Receipt simulation only: no controller, synthesis, player, or daemon.
        self.store.attach_controller('silent-interface-test')

    def test_actual_store_receipts_pause_seek_and_question_keep_exact_source(self):
        value = self.service.command('start', text=SOURCE, title='Example', request_id='start')
        article = value['reading']
        self.assertEqual(article['status'], 'preparing')
        self.assertIsNone(article['active_index'])
        self.assertEqual(''.join(p['text'] for p in article['passages']), SOURCE)
        self.assertEqual(self.service.command('start', text=SOURCE, title='Example', request_id='start')['reading']['id'], article['id'])
        self.assertTrue(self.store.progress(article['id'], article['generation'], 0, 'started'))
        self.assertEqual(self.service.snapshot()['reading']['active_index'], 0)
        paused = self.service.command('pause', reading_id=article['id'], request_id='pause')['reading']
        self.assertEqual(paused['position'], 0)
        self.assertIsNone(paused['active_index'])
        self.assertFalse(self.store.progress(article['id'], article['generation'], 0, 'completed'))
        value = self.service.command('question', message='Explain gravity.', reading_id=article['id'], request_id='question')
        self.assertEqual(value['interaction']['revision'], value['reading']['revision'])
        self.assertEqual(value['reading']['text'], SOURCE)
        self.assertEqual(value['reading']['status'], 'paused')
        value = self.service.command('seek', passage_index=0, reading_id=article['id'], request_id='seek')
        self.assertIsNone(value['interaction'])
        self.assertEqual(value['reading']['text'], SOURCE)

    def test_same_article_answer_is_hidden_after_revision_changes(self):
        from ramble import reading_interaction
        article = self.service.command('start', text=SOURCE, request_id='start')['reading']
        self.service.command('question', message='Why?', reading_id=article['id'], request_id='question')
        with reading_interaction.connect(self.home) as conn:
            conn.execute("UPDATE reading_questions SET status='answered',answer='Separate explanation' WHERE request_id='question'")
        self.assertEqual(self.service.snapshot()['interaction']['answer'], 'Separate explanation')
        value = self.service.command('resume', reading_id=article['id'], request_id='resume')
        self.assertIsNone(value['interaction'])
        self.assertEqual(value['reading']['text'], SOURCE)


if __name__ == '__main__':
    unittest.main()
