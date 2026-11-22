"""Native exact-source reading view; all audio remains in the daemon controller."""
from __future__ import annotations

import threading
import time
import uuid

import AppKit as A
import Foundation as F
import objc

READING_LAYOUT = dict(inset=24, title_y=28, title_height=36, status_y=72, row_height=30,
                      controls_y=112, source_y=160, article_y=190, article_footer=420,
                      question_footer=208, answer_label_footer=162, answer_footer=134,
                      answer_height=110, button_width=116, previous_width=76,
                      follow_width=180, recap_width=150, gap=8, editor_y=196,
                      editor_footer=280, editor_title_y=112, editor_url_y=154,
                      editor_button_footer=64, text_inset=12, preparation_size=18)
READING_TYPE = dict(title=27, body=16, label=13, detail=12, line_spacing=6)
# The parent window calls tick every 200 ms; active reads follow that cadence.
READING_POLL = dict(active=0, idle=1.5)


def utf16_span(source, start, end):
    """Convert immutable Python source offsets to Cocoa's UTF-16 ranges."""
    if type(start) is not int or type(end) is not int or not 0 <= start <= end <= len(source):
        raise ValueError("Invalid source span")
    return len(source[:start].encode('utf-16-le')) // 2, len(source[start:end].encode('utf-16-le')) // 2


def label(value, kind='label'):
    control = A.NSTextField.labelWithString_(value)
    control.setFont_(A.NSFont.systemFontOfSize_weight_(READING_TYPE[kind],
                     A.NSFontWeightSemibold if kind == 'title' else A.NSFontWeightRegular))
    control.setLineBreakMode_(A.NSLineBreakByTruncatingTail)
    return control


def place(control, x, y, width, height):
    control.setFrame_(F.NSMakeRect(x, y, max(1, width), max(1, height)))


class ReadingService:
    def __init__(self, home=None):
        self.home = home

    def store(self):
        from .reading import ReadingStore
        return ReadingStore(self.home)

    def snapshot(self):
        from . import reading_interaction
        value = self.store().snapshot()
        interaction = reading_interaction.snapshot(self.home)
        article = value.get('reading')
        value['interaction'] = interaction if article and interaction and interaction['reading_id'] == article['id'] and interaction.get('revision') == article['revision'] else None
        return value

    def command(self, action, **fields):
        from . import reading_interaction
        if action == 'start':
            self.store().start(**fields)
        elif action in {'question', 'recap'}:
            reading_interaction.submit_question(recap=action == 'recap', home=self.home, **fields)
        elif action == 'restart':
            request_id = fields.pop('request_id')
            self.store().control('seek', passage_index=0, request_id=request_id + '-seek', **fields)
            self.store().control('resume', request_id=request_id + '-resume', **fields)
        else:
            self.store().control(action, **fields)
        return self.snapshot()


class ReadingPage(A.NSView):
    def isFlipped(self):
        return True

    def resizeSubviewsWithOldSize_(self, old):
        objc.super(ReadingPage, self).resizeSubviewsWithOldSize_(old)
        if getattr(self, 'owner', None):
            self.owner.layout()


class ReadingText(A.NSTextView):
    def keyDown_(self, event):
        owner = getattr(self, 'reading_owner', None)
        if owner and not self.isEditable() and event.charactersIgnoringModifiers() == ' ':
            if not event.isARepeat() and owner.play.isEnabled():
                owner.actions.playPause_(None)
            return
        objc.super(ReadingText, self).keyDown_(event)


class ReadingActions(F.NSObject):
    def newArticle_(self, sender):
        self.owner.set_editing(True)

    def cancelArticle_(self, sender):
        self.owner.set_editing(False)

    def startArticle_(self, sender):
        self.owner.submit('start', text=str(self.owner.editor.string()),
                          title=str(self.owner.article_title.stringValue()) or 'Article',
                          source_url=str(self.owner.article_url.stringValue()) or None)

    def playPause_(self, sender):
        article = self.owner.article
        if not article:
            return
        action = 'pause' if article['status'] in {'preparing', 'playing'} else 'restart' if article['status'] == 'completed' else 'resume'
        self.owner.submit(action)

    def seek_(self, sender):
        article = self.owner.article
        if article:
            target = max(0, min(len(article['passages']) - 1, article['position'] + sender.tag()))
            self.owner.submit('seek', passage_index=target)

    def ask_(self, sender):
        self.owner.submit('question', message=str(self.owner.question.stringValue()))

    def recap_(self, sender):
        self.owner.submit('recap')

    def follow_(self, sender):
        self.owner.following = bool(sender.state())
        self.owner.follow_current()

    def scrolled_(self, notification):
        if not self.owner.programmatic_scroll:
            self.owner.following = False
            self.owner.follow.setState_(0)


class ReadingView:
    def __init__(self, service=None):
        self.service = service or ReadingService()
        self.article = None
        self.interaction = None
        self.online = False
        self.editing = True
        self.following = True
        self.programmatic_scroll = False
        self.generation = 0
        self.pending = None
        self.polling = False
        self.busy = False
        self.command_action = None
        self.next_poll = 0
        self.previous_command = None
        self.action_error = None
        self.highlight = None
        self.highlight_signature = None
        self.source_id = None
        self.actions = ReadingActions.alloc().init()
        self.actions.owner = self
        self.view = ReadingPage.alloc().initWithFrame_(F.NSZeroRect)
        self.heading = self.add(label('Reading', 'title'))
        self.status = self.add(label('Loading saved place…', 'detail'))
        self.new_article = self.button('Paste article…', 'newArticle:')
        self.previous = self.button('Previous', 'seek:', tag=-1)
        self.play = self.button('Resume', 'playPause:')
        self.preparation = self.add(A.NSProgressIndicator.alloc().initWithFrame_(F.NSZeroRect))
        self.preparation.setStyle_(A.NSProgressIndicatorStyleSpinning)
        self.preparation.setIndeterminate_(True)
        self.preparation.setDisplayedWhenStopped_(False)
        self.preparation.setAccessibilityLabel_('Preparing reading audio')
        self.next = self.button('Next', 'seek:', tag=1)
        self.follow = self.button('Follow current passage', 'follow:')
        self.follow.setButtonType_(A.NSButtonTypeSwitch)
        self.follow.setState_(1)
        self.recap = self.button('Where were we?', 'recap:')
        self.source_label = self.add(label('Original text', 'detail'))
        self.source_scroll, self.source = self.text_area()
        self.source.reading_owner = self
        self.source.setEditable_(False)
        self.source.setAccessibilityLabel_('Original article text')
        self.question = self.add(A.NSTextField.alloc().initWithFrame_(F.NSZeroRect))
        self.question.setPlaceholderString_('Ask about this article…')
        self.question.setTarget_(self.actions)
        self.question.setAction_('ask:')
        self.ask = self.button('Ask', 'ask:')
        self.answer_label = self.add(label('Explanation · separate from original text', 'detail'))
        self.answer_scroll, self.answer = self.text_area()
        self.answer.setEditable_(False)
        self.answer.setAccessibilityLabel_('Explanation, separate from original text')
        self.article_title = self.add(A.NSTextField.alloc().initWithFrame_(F.NSZeroRect))
        self.article_title.setPlaceholderString_('Article title')
        self.article_title.setAccessibilityLabel_('Article title')
        self.article_url = self.add(A.NSTextField.alloc().initWithFrame_(F.NSZeroRect))
        self.article_url.setPlaceholderString_('Source URL (optional; saved as a reference)')
        self.article_url.setAccessibilityLabel_('Source URL, optional')
        self.editor_scroll, self.editor = self.text_area()
        self.editor.setAllowsUndo_(True)
        self.editor.setAccessibilityLabel_('Paste original article text')
        self.start = self.button('Start reading', 'startArticle:')
        self.cancel = self.button('Cancel', 'cancelArticle:')
        self.reading_controls = [self.previous, self.play, self.preparation, self.next, self.follow, self.recap, self.source_label,
                                 self.source_scroll, self.question, self.ask, self.answer_label, self.answer_scroll]
        self.editor_controls = [self.article_title, self.article_url, self.editor_scroll, self.start, self.cancel]
        self.view.owner = self
        clip = self.source_scroll.contentView()
        clip.setPostsBoundsChangedNotifications_(True)
        F.NSNotificationCenter.defaultCenter().addObserver_selector_name_object_(
            self.actions, 'scrolled:', A.NSViewBoundsDidChangeNotification, clip)
        self.set_editing(True)
        self.layout()

    def add(self, control):
        self.view.addSubview_(control)
        return control

    def button(self, title, action, tag=0):
        control = A.NSButton.buttonWithTitle_target_action_(title, self.actions, action)
        control.setTag_(tag)
        return self.add(control)

    def text_area(self):
        scroll = self.add(A.NSScrollView.alloc().initWithFrame_(F.NSZeroRect))
        scroll.setHasVerticalScroller_(True)
        scroll.setBorderType_(A.NSBezelBorder)
        control = ReadingText.alloc().initWithFrame_(F.NSZeroRect)
        control.setRichText_(False)
        control.setFont_(A.NSFont.systemFontOfSize_(READING_TYPE['body']))
        paragraph = A.NSMutableParagraphStyle.alloc().init()
        paragraph.setLineSpacing_(READING_TYPE['line_spacing'])
        control.setDefaultParagraphStyle_(paragraph)
        control.setTextContainerInset_(F.NSMakeSize(READING_LAYOUT['text_inset'], READING_LAYOUT['text_inset']))
        control.setVerticallyResizable_(True)
        control.setHorizontallyResizable_(False)
        control.setAutoresizingMask_(A.NSViewWidthSizable)
        control.textContainer().setWidthTracksTextView_(True)
        scroll.setDocumentView_(control)
        return scroll, control

    def layout(self):
        self.programmatic_scroll = True
        try:
            token = READING_LAYOUT
            width, height = self.view.bounds().size.width, self.view.bounds().size.height
            x, gap, row = token['inset'], token['gap'], token['row_height']
            content = width - x * 2
            place(self.heading, x, token['title_y'], content - token['button_width'], token['title_height'])
            place(self.new_article, width - x - token['button_width'], token['title_y'], token['button_width'], row)
            place(self.status, x, token['status_y'], content, row)
            cursor = x
            for control, size in ((self.previous, token['previous_width']), (self.play, token['button_width']),
                                  (self.preparation, token['preparation_size']),
                                  (self.next, token['previous_width']), (self.follow, token['follow_width']),
                                  (self.recap, token['recap_width'])):
                if control is self.preparation:
                    place(control, cursor, token['controls_y'] + (row - size) / 2, size, size)
                else:
                    place(control, cursor, token['controls_y'], size, row)
                cursor += size + gap
            place(self.source_label, x, token['source_y'], content, row)
            place(self.source_scroll, x, token['article_y'], content, height - token['article_footer'])
            place(self.question, x, height - token['question_footer'], content - token['previous_width'] - gap, row)
            place(self.ask, width - x - token['previous_width'], height - token['question_footer'], token['previous_width'], row)
            place(self.answer_label, x, height - token['answer_label_footer'], content, row)
            place(self.answer_scroll, x, height - token['answer_footer'], content, token['answer_height'])
            place(self.article_title, x, token['editor_title_y'], content, row)
            place(self.article_url, x, token['editor_url_y'], content, row)
            place(self.editor_scroll, x, token['editor_y'], content, height - token['editor_footer'])
            place(self.start, x, height - token['editor_button_footer'], token['button_width'], row)
            place(self.cancel, x + token['button_width'] + gap, height - token['editor_button_footer'], token['button_width'], row)
        finally:
            self.programmatic_scroll = False

    def set_editing(self, value):
        self.editing = bool(value)
        for control in self.reading_controls:
            control.setHidden_(self.editing)
        for control in self.editor_controls:
            control.setHidden_(not self.editing)
        self.cancel.setEnabled_(bool(self.article))

    def refresh(self):
        if self.polling or self.busy:
            return
        self.polling = True
        self.run(self.service.snapshot, 'poll')

    def run(self, task, action):
        self.generation += 1
        self.pending = None
        generation = self.generation
        def execute():
            try:
                result = task(), None
            except Exception as error:
                result = None, str(error) or 'Reading is unavailable. Try again.'
            if generation == self.generation:
                self.pending = (action, *result)
        threading.Thread(target=execute, name='reading-interface', daemon=True).start()

    def submit(self, action, **fields):
        if self.busy:
            return
        if action != 'start':
            if not self.article:
                return
            fields['reading_id'] = self.article['id']
        signature = (action, repr(sorted(fields.items())))
        if not self.previous_command or self.previous_command[0] != signature:
            self.previous_command = signature, uuid.uuid4().hex
        fields['request_id'] = self.previous_command[1]
        self.busy = True
        self.command_action = action
        self.action_error = None
        self.polling = False
        self.update_controls()
        self.status.setStringValue_('Preparing reading…' if action == 'start' else
                                    'Saving your place and preparing an explanation…' if action in {'question', 'recap'} else 'Updating saved place…')
        self.run(lambda: self.service.command(action, **fields), action)

    def tick(self):
        if self.pending:
            action, result, error = self.pending
            self.pending = None
            self.polling = self.busy = False
            self.command_action = None
            if error:
                if action != 'poll':
                    self.action_error = error
                self.status.setStringValue_(error)
            else:
                if action != 'poll':
                    self.previous_command = None
                if action == 'start':
                    self.set_editing(False)
                if action == 'question':
                    self.question.setStringValue_('')
                self.render(result)
            self.next_poll = time.monotonic() + READING_POLL['active' if self.article and self.article['status'] in {'preparing', 'playing'} else 'idle']
            self.update_controls()
        if time.monotonic() >= self.next_poll:
            self.refresh()

    def render(self, value):
        article = value.get('reading')
        self.online = bool(value.get('controller_online'))
        interaction = value.get('interaction')
        self.interaction = interaction if article and interaction and interaction['reading_id'] == article['id'] and interaction.get('revision') == article['revision'] else None
        changed = article and article['id'] != self.source_id
        self.article = article
        if not article:
            self.status.setStringValue_('Paste an article to start reading.')
            self.set_editing(True)
            self.update_controls()
            return
        if changed:
            self.action_error = None
            self.source_id = article['id']
            self.programmatic_scroll = True
            self.source.setString_(article['text'])
            self.programmatic_scroll = False
            self.highlight = self.highlight_signature = None
            self.following = True
            self.follow.setState_(1)
            if not self.editor.string():
                self.set_editing(False)
        names = dict(preparing='Preparing audio…', playing='Playing', paused='Paused · place saved',
                     completed='Reading complete', failed='Reading stopped')
        status = names.get(article['status'], article['status']) if self.online else 'Reader offline · saved place retained'
        if article.get('error'):
            status += ' · ' + article['error']
        self.status.setStringValue_(self.action_error or status)
        self.status.setToolTip_(status)
        self.heading.setStringValue_(article['title'])
        self.heading.setToolTip_(article['title'])
        active = article.get('active_index') if self.online and article['status'] == 'playing' else None
        position = active if active is not None else min(article['position'], len(article['passages']) - 1)
        saved = article['status'] == 'paused' or not self.online
        place_label = 'Saved place: passage' if saved else 'Passage'
        self.source_label.setStringValue_(f"Original text · {place_label} {position + 1} of {len(article['passages'])}")
        self.source_label.setToolTip_(article.get('source_url') or 'Exact original text')
        signature = (article['id'], active, article['position'], article['status'], self.online)
        if signature != self.highlight_signature:
            storage = self.source.textStorage()
            if self.highlight:
                storage.removeAttribute_range_(A.NSBackgroundColorAttributeName, self.highlight)
                storage.removeAttribute_range_(A.NSUnderlineStyleAttributeName, self.highlight)
            self.highlight = None
            if active is not None or article['status'] == 'paused' or not self.online:
                passage = article['passages'][position]
                self.highlight = utf16_span(article['text'], passage['start'], passage['end'])
                if active is not None:
                    storage.addAttribute_value_range_(A.NSBackgroundColorAttributeName, A.NSColor.selectedTextBackgroundColor(), self.highlight)
                else:
                    storage.addAttribute_value_range_(A.NSBackgroundColorAttributeName, A.NSColor.unemphasizedSelectedTextBackgroundColor(), self.highlight)
            self.highlight_signature = signature
            self.follow_current()
        answer = self.interaction
        if answer:
            statuses = dict(pending='Question queued · reading paused', answering='Preparing an explanation…',
                            answered='Explanation · resume reading when ready', failed=answer.get('error') or 'Explanation unavailable',
                            cancelled='Question cancelled · place retained')
            self.answer_label.setStringValue_(statuses.get(answer['status'], 'Explanation'))
            explanation = answer.get('answer') if answer['status'] == 'answered' else ''
            self.answer.setString_((answer.get('question') or '') + ('\n\n' if explanation else '') + (explanation or ''))
        else:
            self.answer_label.setStringValue_('Explanation · questions pause reading and keep your place')
            self.answer.setString_('')
        self.update_controls()

    def follow_current(self):
        if not self.following or not self.article:
            return
        position = self.article.get('active_index')
        if position is None:
            position = min(self.article['position'], len(self.article['passages']) - 1)
        passage = self.article['passages'][position]
        span = utf16_span(self.article['text'], passage['start'], passage['end'])
        self.programmatic_scroll = True
        self.source.scrollRangeToVisible_(span)
        self.programmatic_scroll = False

    def update_controls(self):
        article = self.article
        playing = self.online and article and article['status'] in {'preparing', 'playing'}
        self.play.setTitle_('Pause' if playing else 'Read again' if article and article['status'] == 'completed' else 'Resume')
        self.play.setEnabled_(bool(article) and not self.busy and self.online)
        preparing = not self.editing and self.online and bool(article) and article['status'] == 'preparing'
        self.preparation.setHidden_(not preparing)
        if preparing:
            self.preparation.startAnimation_(None)
        else:
            self.preparation.stopAnimation_(None)
        self.previous.setEnabled_(bool(article) and not self.busy and article['position'] > 0)
        self.next.setEnabled_(bool(article) and not self.busy and article['position'] < len(article['passages']) - 1)
        answering = self.interaction and self.interaction.get('status') in {'pending', 'answering'}
        for control in (self.ask, self.recap):
            control.setEnabled_(bool(article) and self.online and not self.busy and not answering)
        self.start.setEnabled_(not self.busy)
        self.start.setTitle_('Preparing…' if self.busy and self.command_action == 'start' else 'Start reading')
        self.cancel.setEnabled_(bool(article) and not self.busy)

    def close(self):
        self.generation += 1
        self.preparation.stopAnimation_(None)
        F.NSNotificationCenter.defaultCenter().removeObserver_(self.actions)
        self.view.owner = None
        self.source.reading_owner = None
        self.actions.owner = None
