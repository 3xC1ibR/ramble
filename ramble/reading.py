"""Persistent article reading with exact spans and confirmed playback progress."""
from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import contextmanager
import fcntl
import hashlib
import json
import logging
from pathlib import Path
import re
import sqlite3
import threading
import time
import uuid

from . import config, state, voice_control

log = logging.getLogger('ramble.reading')
MAX_ARTICLE_CHARS = 200_000
MAX_PASSAGE_CHARS = 480
FIRST_PASSAGE_CHARS = 240
MAX_PASSAGES = 10_000
HEARTBEAT_TIMEOUT_S = 5.0
MAX_AUDIO_FILE_BYTES = 16 * 1024 * 1024
ACTIVE_STATUSES = ('preparing', 'playing')
ABBREVIATION_END = re.compile(r'\b(?:Mr|Mrs|Ms|Dr|Prof|Sr|Jr|St|vs|etc|e\.g|i\.e|[A-Z])\.$')
SCHEMA = """
CREATE TABLE IF NOT EXISTS readings (
 id TEXT PRIMARY KEY, title TEXT NOT NULL, source_url TEXT, text TEXT NOT NULL,
 passages TEXT NOT NULL, position INTEGER NOT NULL DEFAULT 0, active_index INTEGER,
 status TEXT NOT NULL, error TEXT, revision INTEGER NOT NULL DEFAULT 1,
 generation INTEGER NOT NULL DEFAULT 1, heard_indices TEXT NOT NULL DEFAULT '[]',
 last_started_index INTEGER,
 updated_at REAL NOT NULL, intent_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS reading_state (
 singleton INTEGER PRIMARY KEY CHECK(singleton=1), current_id TEXT,
 controller_token TEXT, heartbeat_at REAL NOT NULL DEFAULT 0
);
INSERT OR IGNORE INTO reading_state(singleton) VALUES (1);
CREATE TABLE IF NOT EXISTS reading_requests (
 request_id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, created_at REAL NOT NULL
);
"""


def passages(text: str) -> list[dict]:
    """Partition the original text without normalizing or dropping any characters."""
    boundaries = set()
    for match in re.finditer(r'[.!?][\"\'”’\)\]]*(?:\s+|$)|\n+', text):
        # Preserve common titles, initials, and decimal values within a sentence.
        # A bounded suffix avoids rescanning the entire article for every period.
        if text[match.start()] == '.' and ABBREVIATION_END.search(
                text[max(0, match.start() - 16):match.start() + 1]):
            continue
        boundaries.add(match.end())
    boundaries.add(len(text))
    result = []
    start = 0
    for end in sorted(boundaries):
        if not text[start:end].strip():
            continue
        content_start = start + len(text[start:end]) - len(text[start:end].lstrip())
        content_end = start + len(text[start:end].rstrip())
        while content_end - content_start > (FIRST_PASSAGE_CHARS if not result else MAX_PASSAGE_CHARS):
            limit = content_start + (FIRST_PASSAGE_CHARS if not result else MAX_PASSAGE_CHARS)
            spaces = list(re.finditer(r'\s+', text[content_start:limit]))
            cut = content_start + spaces[-1].end() if spaces else limit
            result.append(dict(index=len(result), text=text[start:cut], start=start, end=cut))
            start = cut
            content_start = start + len(text[start:end]) - len(text[start:end].lstrip())
        result.append(dict(index=len(result), text=text[start:end], start=start, end=end))
        start = end
    if start < len(text) and result:
        result[-1]['end'] = len(text)
        result[-1]['text'] = text[result[-1]['start']:]
    if len(result) > MAX_PASSAGES:
        raise ValueError(f'Use at most {MAX_PASSAGES:,} passages in one article.')
    return result


class ReadingStore:
    def __init__(self, home=None):
        self.home = Path(home) if home is not None else config.HOME
        self.home.mkdir(parents=True, exist_ok=True)
        self.path = self.home / 'reading.sqlite3'
        with self.connect() as conn:
            conn.executescript(SCHEMA)
            if 'last_started_index' not in {row['name'] for row in conn.execute('PRAGMA table_info(readings)')}:
                conn.execute('BEGIN IMMEDIATE')
                # Several interface processes can discover an older store together.
                if 'last_started_index' not in {row['name'] for row in conn.execute('PRAGMA table_info(readings)')}:
                    conn.execute('ALTER TABLE readings ADD COLUMN last_started_index INTEGER')

    @contextmanager
    def connect(self, *, transaction=False):
        conn = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute('PRAGMA journal_mode=WAL')
            if transaction:
                conn.execute('BEGIN IMMEDIATE')
            yield conn
            if conn.in_transaction:
                conn.commit()
        except BaseException:
            if conn.in_transaction:
                conn.rollback()
            raise
        finally:
            conn.close()

    def _snapshot(self, conn):
        metadata = conn.execute('SELECT * FROM reading_state WHERE singleton=1').fetchone()
        row = conn.execute('SELECT * FROM readings WHERE id=?', (metadata['current_id'],)).fetchone()
        reading = dict(row) if row else None
        if reading:
            reading['passages'] = json.loads(reading['passages'])
            reading['heard_indices'] = json.loads(reading['heard_indices'])
        return {'reading': reading, 'controller_online': bool(metadata['controller_token']
                and time.time() - metadata['heartbeat_at'] < HEARTBEAT_TIMEOUT_S)}

    def snapshot(self):
        with self.connect() as conn:
            return self._snapshot(conn)

    @contextmanager
    def paused_revision(self, reading_id, revision):
        """Hold the reading revision stable through a question-result commit.

        Callers holding another store transaction acquire their interaction
        transaction first, then this guard, to preserve a single lock order.
        """
        with self.connect(transaction=True) as conn:
            row = conn.execute('SELECT r.id,r.revision,r.status FROM readings r JOIN reading_state s '
                               'ON r.id=s.current_id').fetchone()
            yield bool(row and row['id'] == reading_id and row['revision'] == revision
                       and row['status'] == 'paused')

    @staticmethod
    def _request(conn, request_id, payload):
        if request_id is None:
            return False
        if not isinstance(request_id, str) or not 1 <= len(request_id) <= 240:
            raise ValueError('Use a request ID of 1–240 characters.')
        fingerprint = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
        row = conn.execute('SELECT fingerprint FROM reading_requests WHERE request_id=?',
                           (request_id,)).fetchone()
        if row:
            if row['fingerprint'] != fingerprint:
                raise ValueError('This request ID belongs to a different reading operation.')
            return True
        conn.execute('INSERT INTO reading_requests VALUES (?,?,?)',
                     (request_id, fingerprint, time.time()))
        return False

    def start(self, text, *, title='Article', source_url=None, request_id=None):
        if not isinstance(text, str) or not text.strip() or len(text) > MAX_ARTICLE_CHARS:
            raise ValueError(f'Use nonempty article text of at most {MAX_ARTICLE_CHARS:,} characters.')
        if not isinstance(title, str) or not title.strip() or len(title) > 500:
            raise ValueError('Use a title of 1–500 characters.')
        if source_url is not None and (not isinstance(source_url, str) or len(source_url) > 4096):
            raise ValueError('Source URL must be text of at most 4,096 characters.')
        spans = passages(text)
        with self.connect(transaction=True) as conn:
            if self._request(conn, request_id, ['start', text, title, source_url]):
                return self._snapshot(conn)
            now = time.time()
            conn.execute("UPDATE readings SET status='paused',active_index=NULL,revision=revision+1,"
                         "generation=generation+1,updated_at=? WHERE id=(SELECT current_id FROM reading_state) "
                         "AND status IN ('preparing','playing')", (now,))
            reading_id = uuid.uuid4().hex
            conn.execute("INSERT INTO readings(id,title,source_url,text,passages,status,updated_at,intent_at) "
                         "VALUES (?,?,?,?,?,'preparing',?,?)",
                         (reading_id, title, source_url, text, json.dumps(spans), now, now))
            conn.execute('UPDATE reading_state SET current_id=? WHERE singleton=1', (reading_id,))
            return self._snapshot(conn)

    def control(self, action, *, reading_id=None, passage_index=None, request_id=None):
        if action not in {'pause', 'resume', 'seek', 'restart'}:
            raise ValueError('Choose pause, resume, seek or restart.')
        with self.connect(transaction=True) as conn:
            if self._request(conn, request_id, [action, reading_id, passage_index]):
                return self._snapshot(conn)
            current = self._snapshot(conn)['reading']
            if not current or (reading_id is not None and reading_id != current['id']):
                raise ValueError('That reading is no longer selected. Refresh the reading view.')
            position = current['position']
            status = 'paused'
            if action in {'seek', 'restart'}:
                position = 0 if action == 'restart' else passage_index
                if type(position) is not int or not 0 <= position < len(current['passages']):
                    raise ValueError('Choose a passage from this article.')
            elif action == 'resume':
                status = 'preparing' if position < len(current['passages']) else 'completed'
            now = time.time()
            conn.execute('UPDATE readings SET position=?,active_index=NULL,status=?,error=NULL,'
                         'revision=revision+1,generation=generation+1,updated_at=?,intent_at=? WHERE id=?',
                         (position, status, now, now, current['id']))
            return self._snapshot(conn)

    def valid(self, reading_id, generation, index=None):
        with self.connect() as conn:
            row = conn.execute('SELECT r.generation,r.status,r.position FROM readings r JOIN reading_state s '
                               'ON r.id=s.current_id WHERE r.id=?', (reading_id,)).fetchone()
        return bool(row and row['generation'] == generation and row['status'] in ACTIVE_STATUSES
                    and (index is None or row['position'] == index))

    def progress(self, reading_id, generation, index, event, *, error=None):
        """Apply only a receipt from the current generation and exact current passage."""
        if event not in {'started', 'completed', 'paused', 'failed'}:
            raise ValueError('Invalid reading progress event.')
        with self.connect(transaction=True) as conn:
            current = self._snapshot(conn)['reading']
            if (not current or current['id'] != reading_id or current['generation'] != generation
                    or current['position'] != index or current['status'] not in ACTIVE_STATUSES):
                return False
            status, position, active = current['status'], index, None
            heard = current['heard_indices']
            last_started = current['last_started_index']
            generation_change = 0
            if event == 'started':
                if current['active_index'] == index:
                    return True
                status, active = 'playing', index
                last_started = index
            elif event == 'completed':
                if current['active_index'] != index:
                    return False
                # Preserve recency as well as membership: a repeated passage
                # is the latest confirmed thing heard for recap and repeat.
                heard = [heard_index for heard_index in heard if heard_index != index]
                heard.append(index)
                position += 1
                status = 'completed' if position == len(current['passages']) else 'preparing'
            else:
                status = 'paused' if event == 'paused' else 'failed'
                generation_change = 1
            conn.execute('UPDATE readings SET position=?,active_index=?,status=?,error=?,heard_indices=?,last_started_index=?,'
                         'revision=revision+1,generation=generation+?,updated_at=? WHERE id=?',
                         (position, active, status, error, json.dumps(heard), last_started, generation_change,
                          time.time(), reading_id))
            return True

    def attach_controller(self, token):
        with self.connect(transaction=True) as conn:
            # A new process cannot know whether an interrupted passage was heard.
            # Only an explicit later resume may replay it.
            conn.execute("UPDATE readings SET status='paused',active_index=NULL,revision=revision+1,"
                         "generation=generation+1,updated_at=? WHERE status IN ('preparing','playing')",
                         (time.time(),))
            conn.execute('UPDATE reading_state SET controller_token=?,heartbeat_at=? WHERE singleton=1',
                         (token, time.time()))

    def heartbeat(self, token):
        with self.connect() as conn:
            conn.execute('UPDATE reading_state SET heartbeat_at=? WHERE controller_token=?',
                         (time.time(), token))

    def detach_controller(self, token):
        with self.connect(transaction=True) as conn:
            row = conn.execute('SELECT controller_token FROM reading_state').fetchone()
            if row['controller_token'] != token:
                return
            conn.execute("UPDATE readings SET status='paused',active_index=NULL,revision=revision+1,"
                         "generation=generation+1,updated_at=? WHERE status IN ('preparing','playing')",
                         (time.time(),))
            conn.execute('UPDATE reading_state SET controller_token=NULL,heartbeat_at=0')


class ReadingController:
    """Poll controls independently of blocking synthesis and serialized playback."""
    def __init__(self, cfg, store=None):
        # Companion uses the store under a standard-library-only interpreter.
        # Load audio dependencies only in the daemon that owns playback.
        from . import speech
        self.speech = speech
        self.cfg = cfg
        self.store = store if store is not None else ReadingStore()
        self.home = self.store.home
        self.speech_active = lambda: False
        self.can_play = lambda: True
        self.cache = self.home / 'reading_audio'
        self.cache.mkdir(mode=0o700, parents=True, exist_ok=True)
        profile = {key: value for key, value in cfg.items()
                   if key.startswith(('tts_', 'elevenlabs_')) and 'key' not in key}
        self.profile = hashlib.sha256(json.dumps(profile, sort_keys=True).encode()).hexdigest()[:16]
        self.preparation = ThreadPoolExecutor(max_workers=2, thread_name_prefix='reading-synthesis')
        self.current_preparation = ThreadPoolExecutor(max_workers=1, thread_name_prefix='reading-current')
        self.current_job = None
        self.speculative_jobs = set()
        self.output = ThreadPoolExecutor(max_workers=1, thread_name_prefix='reading-output')
        self.pending = {}
        self.playback = None
        self.playback_key = None
        self.stopping = threading.Event()
        # Current, two prefetched passages, and one cancelling old output may
        # coexist. Budget for these protected files before retaining older audio.
        self.cache_files = max(4, min(32, int(cfg.get('reading_cache_files', 8))))
        self.cache_bytes = max(4 * MAX_AUDIO_FILE_BYTES, min(256 * 1024 * 1024,
                               int(cfg.get('reading_cache_bytes', 64 * 1024 * 1024))))
        self.prefetch = max(0, min(2, int(cfg.get('reading_prefetch_passages', 2))))

    def _audio_allowed(self, since):
        capture = state.read(self.home)
        voice = voice_control.read(self.home)
        return (self.cfg.get('fast_speak', True) and not state.is_paused(capture)
                and voice['enabled'] and capture.get('last_paused_at', 0) <= since
                and voice.get('changed_at', 0) <= since)

    def _path(self, reading, index):
        return self.cache / f"{reading['id']}-{self.profile}-{index:05d}.wav"

    def _cached(self, reading, index):
        path = self._path(reading, index)
        if path.is_file():
            if path.stat().st_size <= MAX_AUDIO_FILE_BYTES:
                try:
                    return self.speech.SpeechResult(path, 'cache', self.speech._duration(path))
                except (ValueError, RuntimeError, OSError):
                    pass
            path.unlink(missing_ok=True)
        return None

    def _prepare(self, reading, index, stop):
        path = self._path(reading, index)
        def cancelled():
            return (stop.is_set() or self.stopping.is_set()
                    or not self.store.valid(reading['id'], reading['generation'])
                    or not self._audio_allowed(reading['intent_at']))
        if cancelled():
            raise state.PausedError('Reading preparation cancelled')
        cached = self._cached(reading, index)
        if cached is not None:
            return cached
        temporary = self.cache / f'.preparing-{uuid.uuid4().hex}.wav'
        try:
            result = self.speech.synthesize(reading['passages'][index]['text'].strip(), self.cfg, output=temporary)
            if cancelled():
                raise state.PausedError('Reading preparation cancelled')
            if temporary.stat().st_size > MAX_AUDIO_FILE_BYTES:
                raise ValueError('Prepared reading audio exceeds the file size limit.')
            temporary.replace(path)
            return self.speech.SpeechResult(path, result.engine, result.duration)
        finally:
            temporary.unlink(missing_ok=True)

    def _prune_cache(self, protected=()):
        files = sorted((p for p in self.cache.glob('*.wav') if not p.name.startswith('.')),
                       key=lambda p: p.stat().st_mtime, reverse=True)
        count = sum(path in protected for path in files)
        size = sum(path.stat().st_size for path in files if path in protected)
        for path in files:
            if path in protected:
                continue
            length = path.stat().st_size
            if count < self.cache_files and size + length <= self.cache_bytes:
                count += 1
                size += length
            else:
                path.unlink(missing_ok=True)

    def _play(self, reading, result, stop):
        index = reading['position']
        def cancelled():
            return (stop.is_set() or self.stopping.is_set()
                    or not self.store.valid(reading['id'], reading['generation'], index)
                    or not self._audio_allowed(reading['intent_at'])
                    or self.speech_active() or not self.can_play())
        def started():
            if cancelled() or not self.store.progress(reading['id'], reading['generation'], index, 'started'):
                raise state.PausedError('Reading output superseded')
        if cancelled():
            raise state.PausedError('Reading output cancelled')
        self.speech.play_prepared(result.path, self.cfg, cancelled=cancelled, origin='reading',
                                  on_playback_start=started, home=self.home)
        if cancelled():
            raise state.PausedError('Reading output cancelled')
        if not self.store.progress(reading['id'], reading['generation'], index, 'completed'):
            if self.store.valid(reading['id'], reading['generation'], index):
                raise RuntimeError('Reading output completed without a start receipt.')

    def tick(self, stop):
        current = self.store.snapshot()['reading']
        if self.playback is not None and self.playback.done():
            key = self.playback_key
            try:
                self.playback.result()
            except state.PausedError:
                self.store.progress(*key, 'paused')
            except Exception:
                log.exception('Reading output failed')
                self.store.progress(*key, 'failed', error='Audio playback failed. Resume to retry this passage.')
            self.playback = self.playback_key = None
            current = self.store.snapshot()['reading']
        if current and current['status'] in ACTIVE_STATUSES and not self._audio_allowed(current['intent_at']):
            self.store.progress(current['id'], current['generation'], current['position'], 'paused')
            current = self.store.snapshot()['reading']
        for key, future in list(self.pending.items()):
            valid = bool(current and key[:2] == (current['id'], current['generation'])
                         and current['status'] in ACTIVE_STATUSES and key[2] >= current['position'])
            if not valid and (future.cancel() or future.done()):
                self.pending.pop(key)
        protected = set()
        if current:
            protected.update(self._path(current, i) for i in range(current['position'],
                             min(len(current['passages']), current['position'] + 3)))
        # Do not delete an interrupted player's file before it confirms termination.
        if self.playback_key:
            protected.add(self.cache / f'{self.playback_key[0]}-{self.profile}-{self.playback_key[2]:05d}.wav')
        self._prune_cache(protected)
        if not current or current['status'] not in ACTIVE_STATUSES or stop.is_set() or self.stopping.is_set():
            return False
        index = current['position']
        key = (current['id'], current['generation'], index)
        if key not in self.pending:
            cached = self._cached(current, index)
            if cached is not None:
                # Resume of prepared audio must never queue behind stale provider
                # calls. A completed future participates in the same delivery path.
                self.pending[key] = Future()
                self.pending[key].set_result(cached)
            elif self.current_job is None or self.current_job.done():
                # One fixed current-passage lane stays independent of the two
                # speculative workers, without accumulating per-generation pools.
                self.current_job = self.current_preparation.submit(self._prepare, current, index, stop)
                self.pending[key] = self.current_job
        future = self.pending.get(key)
        if self.playback is None and future is not None and future.done():
            try:
                result = future.result()
            except state.PausedError:
                self.store.progress(*key, 'paused')
                self.pending.pop(key)
                return True
            except Exception:
                log.exception('Reading synthesis failed')
                self.store.progress(*key, 'failed', error='Speech preparation failed. Resume to retry this passage.')
                self.pending.pop(key)
                return True
            if not self.speech_active() and self.can_play():
                self.playback_key = key
                self.playback = self.output.submit(self._play, current, result, stop)
        if current['status'] == 'playing' and current['active_index'] == index:
            self.speculative_jobs = {future for future in self.speculative_jobs if not future.done()}
            for next_index in range(index + 1, min(len(current['passages']), index + self.prefetch + 1)):
                next_key = (current['id'], current['generation'], next_index)
                if next_key not in self.pending and len(self.speculative_jobs) < 2:
                    future = self.preparation.submit(self._prepare, current, next_index, stop)
                    self.pending[next_key] = future
                    self.speculative_jobs.add(future)
        return True

    def close(self):
        self.stopping.set()
        for future in self.pending.values():
            future.cancel()
        self.preparation.shutdown(wait=False, cancel_futures=True)
        self.current_preparation.shutdown(wait=False, cancel_futures=True)
        self.output.shutdown(wait=False, cancel_futures=True)

    def run_forever(self, stop):
        token = uuid.uuid4().hex
        with (self.home / 'reading.lock').open('a') as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                log.error('Another reading controller owns this home')
                self.close()
                return
            self.store.attach_controller(token)
            for path in self.cache.glob('.preparing-*.wav'):
                path.unlink(missing_ok=True)
            heartbeat_at = 0.0
            try:
                while not stop.is_set():
                    if time.monotonic() - heartbeat_at >= 1:
                        self.store.heartbeat(token)
                        heartbeat_at = time.monotonic()
                    try:
                        self.tick(stop)
                    except Exception:
                        log.exception('Reading controller iteration failed')
                    stop.wait(.05)
            finally:
                self.close()
                self.store.detach_controller(token)
