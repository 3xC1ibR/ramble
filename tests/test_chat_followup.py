"""Late quick-chat admission: throwaway data, fake models/workers, silent speech."""
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

from ramble import chat_followup, config, state
from ramble.fastlane import FastLane
from ramble.jev import JevError
from ramble.router import Router
from ramble.sessions import Sessions
from ramble.session_store import SessionStore
from ramble.speech_queue import SpeechQueue
from ramble.transcribe import Transcriber, stamp

FIRST = "Hey Ramble, I don't actually have a request for you. I just want you to respond. I'm trying to get a sense of the latency."
REPLY = 'Here I am. A very elaborate stopwatch.'
FOLLOWUP = 'If I say something right now, will you respond? Like, will you continue from the last, uh... The last response that you sent?'


class ChatFollowupTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix='ramble-test-chat-', dir='/tmp')
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name)
        for name, path in {'HOME':self.home, 'DB_PATH':self.home/'ramble.db',
                           'STATE_PATH':self.home/'state.json', 'DIGEST_DIR':self.home/'digest',
                           'TRIAGE_DIR':self.home/'triage'}.items():
            self.enterContext(patch.object(config, name, path))
        self.cfg = dict(config.DEFAULTS, dispatch_daily_cap=0, fast_speak=False,
                        speak_questions=False, audio_cues_enabled=False, notify=False, fast_coalesce_s=.001,
                        jev_api_key_file=None)
        (self.home/'config.json').write_text(json.dumps(self.cfg))
        self.store = SessionStore(self.home)
        self.conn = self.enterContext(self.store.connect())
        self.backend = Mock()
        self.backend.create.side_effect = AssertionError('must never launch')
        self.sessions = Sessions(self.cfg, self.backend, self.store)
        self.sessions.speech = SpeechQueue(self.cfg, self.store)
        self.fast = FastLane(self.cfg)
        self.fast.sessions = self.sessions
        self.fast.jev = Mock(available=True)
        self.fast.jev.classify.return_value = {'directed':.87, 'related':.89, 'accept':True}
        self.fast.run_codex = Mock(return_value={'reply':'Yes, we can continue.', 'handoff':False, 'route':None})
        self.now = time.time()
        self.conversation = [{'at':stamp(self.now-125),'speaker':'the user','text':FIRST},
                             {'at':stamp(self.now-110),'speaker':'Ramble','text':REPLY}]
        self.anchor()

    def anchor(self, age=110, status='played'):
        self.conn.execute("INSERT OR REPLACE INTO session_speech(id,text,kind,priority,epoch,status,created_at,expires_at) VALUES ('previous',?,'reply',0,0,?,?,?)",
                          (REPLY, status, self.now-age, self.now-age+120))
        with patch('ramble.chat_followup.time.time', return_value=self.now-age):
            chat_followup.save(self.conn, self.cfg, 'previous', self.conversation, 'previous')

    def transcribe(self, text=FOLLOWUP, identifier='candidate', start=None):
        start = self.now if start is None else start
        self.conn.execute("INSERT INTO segments(id,started_at,ended_at,duration,audio_path) VALUES (?,?,?,2,?)",
                          (identifier, start, start+2, str(self.home/(identifier+'.flac'))))
        row = self.conn.execute('SELECT * FROM segments WHERE id=?', (identifier,)).fetchone()
        transcriber = Transcriber(self.cfg, threading.Event())
        transcriber.on_utterance = self.fast.offer
        with patch.object(transcriber, 'transcribe_path', return_value={'text':text,'segments':[{'no_speech_prob':0,'avg_logprob':-.1}]}):
            transcriber.process(self.conn, row)
        return self.conn.execute('SELECT * FROM segments WHERE id=?', (identifier,)).fetchone()

    def respond(self, item=None):
        self.fast.respond_candidate(self.conn, item or self.fast.candidates.get_nowait(), threading.Event())

    def test_exact_observed_late_followup_replies_with_previous_exchange(self):
        self.transcribe()
        self.assertTrue(self.fast.q.empty())
        self.assertEqual(self.fast.request_until, 0)
        self.respond()
        self.assertIn(REPLY, self.fast.run_codex.call_args.args[0])
        self.assertIn(FOLLOWUP, self.fast.run_codex.call_args.args[0])
        self.assertEqual(self.fast.jev.classify.call_args.args[:2], (self.conversation,FOLLOWUP))
        self.assertEqual(self.conn.execute("SELECT status FROM session_claims WHERE segment_id='candidate'").fetchone()[0], 'handled')
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM work_sessions').fetchone()[0],0)
        self.backend.create.assert_not_called()

    def test_split_utterance_is_classified_and_answered_together(self):
        first, second = FOLLOWUP.split(' The last')
        self.transcribe(first, 'first')
        self.transcribe('The last'+second, 'second', self.now+5)
        self.respond()
        self.assertEqual(self.fast.jev.classify.call_count,1)
        self.assertEqual(self.fast.jev.classify.call_args.args[1], FOLLOWUP)
        self.assertEqual(self.fast.run_codex.call_count,1)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM session_claims WHERE status='handled'").fetchone()[0],2)

    def test_continuation_arriving_during_jev_is_reclassified(self):
        self.transcribe('If I say something right now, will you respond?', 'first')
        def check(*args):
            if self.fast.jev.classify.call_count == 1:
                self.transcribe('Will you continue from the last response?', 'second', self.now+5)
            return {'directed':.9,'related':.9,'accept':True}
        self.fast.jev.classify.side_effect = check
        self.respond()
        self.assertEqual(self.fast.jev.classify.call_count,2)
        self.assertEqual(self.fast.run_codex.call_count,1)
        self.assertIn('Will you continue', self.fast.jev.classify.call_args.args[1])

    def test_rejected_ambient_is_reserved_then_returned_to_slow_triage(self):
        row = self.transcribe('The stopwatch is on the desk.')
        router = Router(self.cfg, threading.Event())
        thought = self.conn.execute('SELECT * FROM thoughts WHERE id=?',(row['thought_id'],)).fetchone()
        with patch.object(router,'run_triage') as triage:
            router.triage(self.conn,thought)
        triage.assert_not_called()
        self.fast.jev.classify.return_value = {'directed':.16,'related':.32,'accept':False}
        with patch.object(self.fast.cues,'emit') as cue:
            self.respond()
        cue.assert_not_called()
        self.fast.run_codex.assert_not_called()
        self.assertFalse(self.conn.execute('SELECT 1 FROM session_claims').fetchone())
        self.assertIn('stopwatch is on the desk',router.unclaimed_text(self.conn,thought))
        self.assertEqual(self.sessions.pending_inputs(),[])

    def test_error_is_silent_and_releases_candidate(self):
        self.transcribe()
        self.fast.jev.classify.side_effect = JevError('timeout')
        with self.assertLogs('ramble.fast',level='WARNING'), patch.object(self.fast.cues,'emit') as cue:
            self.respond()
        cue.assert_not_called()
        self.fast.run_codex.assert_not_called()
        self.assertEqual(self.sessions.pending_inputs(),[])
        self.assertEqual(self.fast.unavailable_until,0) # Jev failure cannot disable direct Codex replies.

    def test_disabled_missing_credentials_and_unheard_replies_stay_passive(self):
        for status in ('pending','failed','cancelled','suppressed','uncertain'):
            self.anchor(status=status)
            self.transcribe(identifier=status)
            self.assertTrue(self.fast.candidates.empty())
        self.anchor()
        self.cfg['chat_followup_s']=0
        self.transcribe(identifier='disabled')
        self.cfg['chat_followup_s']=600
        self.fast.jev.available=False
        self.transcribe(identifier='no-key')
        self.assertTrue(self.fast.candidates.empty())

    def test_automatic_twenty_second_window_does_not_call_jev(self):
        self.conn.execute("INSERT INTO speech_playbacks VALUES ('p',?,?,'unused.wav')",(self.now-10,self.now-5))
        self.transcribe()
        self.assertEqual(self.fast.q.get_nowait()['why'],'follow-up')
        self.assertTrue(self.fast.candidates.empty())
        self.fast.jev.classify.assert_not_called()

    def test_old_context_is_not_extended_by_ambient_speech(self):
        self.anchor(age=599)
        self.transcribe(identifier='early')
        self.fast.jev.classify.return_value={'accept':False}
        self.respond()
        self.transcribe(identifier='late',start=self.now+2)
        self.assertTrue(self.fast.candidates.empty())

    def test_restart_preserves_candidate_context_and_does_not_repeat_handled_input(self):
        self.transcribe()
        item,=self.sessions.pending_inputs()
        restored=FastLane(self.cfg); restored.sessions=self.sessions
        restored.jev=self.fast.jev; restored.run_codex=self.fast.run_codex
        self.fast=restored
        self.respond(item)
        self.assertIn(REPLY,self.fast.run_codex.call_args.args[0])
        self.respond(item)
        self.assertEqual(self.fast.run_codex.call_count,1)
        self.assertEqual(self.sessions.pending_inputs(),[])

    def test_new_address_supersedes_inflight_candidate(self):
        self.transcribe()
        def check(*args):
            self.transcribe('Ramble, what time is it?', 'new', self.now+5)
            return {'accept':True}
        self.fast.jev.classify.side_effect=check
        self.respond()
        self.fast.run_codex.assert_not_called()
        self.assertEqual(self.fast.q.get_nowait()['seg']['id'],'new')

    def test_pause_and_expired_queue_prevent_admission(self):
        self.transcribe()
        item=self.fast.candidates.get_nowait()
        item['chat']['candidate_expires_at']=self.now-1
        self.respond(item)
        self.fast.jev.classify.assert_not_called()
        self.transcribe(identifier='paused')
        def check(*args):
            state.write(False,time.time()+3600)
            return {'accept':True}
        self.fast.jev.classify.side_effect=check
        self.respond()
        self.fast.run_codex.assert_not_called()

    def test_focus_stop_and_quiet_changes_prevent_late_admission(self):
        self.conn.execute("INSERT INTO work_sessions(id,name,goal,cwd,kind,status,created_at,updated_at) VALUES ('owner','old task','old goal',?,'interactive','idle',?,?)",(str(self.home),self.now-300,self.now-300))
        for change in ('epoch=epoch+1','speech_epoch=speech_epoch+1','foreground=0'):
            with self.subTest(change=change):
                self.conn.execute('UPDATE work_sessions SET foreground=1')
                self.anchor()
                self.transcribe(identifier=change)
                def check(*args):
                    self.conn.execute('UPDATE work_sessions SET '+change)
                    return {'accept':True}
                self.fast.jev.classify.side_effect=check
                self.respond()
        self.fast.run_codex.assert_not_called()

    def test_admitted_discussion_can_grow_into_work_with_same_context(self):
        # Fake backend only; the on-disk cap remains zero and no worker is run.
        self.cfg.update(dispatch_daily_cap=1, work_session_root=str(self.home))
        self.transcribe("Let's work through ways to reduce that delay.")
        self.fast.run_codex.return_value={'reply':'Working on it.','handoff':False,'route':{'action':'work','cwd':str(self.home),'name':'Latency'}}
        self.respond()
        command=self.conn.execute('SELECT payload FROM session_commands').fetchone()
        self.assertIsNotNone(command)
        self.assertIn(REPLY,command[0])
        self.assertIsNone(chat_followup.current(self.conn,self.cfg,self.now))
        self.backend.create.assert_not_called()

    def test_focus_change_during_response_generation_cancels_work(self):
        self.transcribe()
        def answer(*args, **kwargs):
            self.conn.execute("INSERT INTO work_sessions(id,name,goal,cwd,kind,status,created_at,updated_at) VALUES ('new','new task','new goal',?,'interactive','idle',?,?)",(str(self.home),self.now,self.now))
            self.assertTrue(kwargs['cancelled']())
            return {'reply':'Starting work','handoff':True,'route':None}
        self.fast.run_codex.side_effect=answer
        self.respond()
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM session_commands').fetchone()[0],0)
        self.assertEqual(self.conn.execute("SELECT status FROM session_inputs WHERE segment_id='candidate'").fetchone()[0],'failed')

    def test_focus_change_and_return_cannot_revive_old_candidate(self):
        for sid in ('one','two'):
            self.conn.execute("INSERT INTO work_sessions(id,name,goal,cwd,kind,status,foreground,created_at,updated_at) VALUES (?,?,?,?,'interactive','idle',?,?,?)",
                              (sid,sid,sid,str(self.home),int(sid=='one'),self.now,self.now))
        self.anchor()
        self.transcribe()
        def check(*args):
            self.store.control('two','focus','switch-away')
            self.store.control('one','focus','switch-back')
            return {'accept':True}
        self.fast.jev.classify.side_effect=check
        self.respond()
        self.fast.run_codex.assert_not_called()

    def test_restart_after_admission_cannot_execute_expired_request(self):
        self.transcribe()
        with patch.object(self.fast,'respond'):
            self.respond()
        item,=self.sessions.pending_inputs()
        self.assertEqual(item['why'],'chat-followup')
        self.assertNotIn('chat',item)
        item['chat_admission']['candidate_expires_at']=self.now-1
        self.fast.respond(self.conn,[item])
        self.fast.run_codex.assert_not_called()
        self.assertEqual(self.sessions.pending_inputs(),[])

    def test_real_reply_records_anchor_but_waits_for_playback(self):
        chat_followup.clear(self.conn)
        self.transcribe('Ramble, are you listening?', 'direct', self.now-5)
        self.fast.respond(self.conn,[self.fast.q.get_nowait()])
        self.assertIsNone(chat_followup.current(self.conn,self.cfg,time.time()))
        self.conn.execute("UPDATE session_speech SET status='played' WHERE status='pending'")
        saved=chat_followup.current(self.conn,self.cfg,time.time())
        self.assertEqual(saved['conversation'][-1]['text'],'Yes, we can continue.')

    def test_context_stays_bounded_and_ordered_across_midnight(self):
        entries=[{'at':'23:59:55','timestamp':self.now-5,'speaker':'the user','text':'before'},
                 {'at':'00:00:01','timestamp':self.now-1,'speaker':'Ramble','text':'after'},
                 {'at':'23:00:00','timestamp':self.now-601,'speaker':'the user','text':'too old'}]
        result=self.fast.conversation_for([{'conversation':entries}])
        self.assertEqual([e['text'] for e in result],['before','after'])
        chat_followup.save(self.conn,self.cfg,'bounded',entries+[{'at':'now','timestamp':self.now,'speaker':'the user','text':'x'*5000}],'previous')
        saved=chat_followup.current(self.conn,self.cfg,time.time())
        self.assertEqual([e['text'] for e in saved['conversation'][:-1]],['before','after'])
        self.assertEqual(len(saved['conversation'][-1]['text']),4000)

    def test_candidate_does_not_extend_unrelated_background_thought(self):
        chat_followup.clear(self.conn)
        ambient=self.transcribe('I should consider a better desk layout.','ambient',self.now-100)
        self.anchor()
        self.transcribe()
        router=Router(self.cfg,threading.Event())
        with patch('ramble.router.state.is_listening',return_value=True):
            router.close_stale(self.conn)
        thought=self.conn.execute('SELECT * FROM thoughts WHERE id=?',(ambient['thought_id'],)).fetchone()
        self.assertEqual(thought['status'],'closed')
        self.assertEqual(thought['ended_at'],self.now-98)


if __name__ == '__main__':
    unittest.main()
