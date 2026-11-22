import sqlite3
import sys
import unittest

from ramble import output_activity as oa


class FakeReader:
    def __init__(self, pids):
        self.pids = pids

    def output_pids(self):
        return set(self.pids)


def conn():
    c = sqlite3.connect(":memory:", isolation_level=None)
    c.execute(oa.SCHEMA)
    return c


class MonitorTests(unittest.TestCase):
    def test_sample_excludes_ramble_processes(self):
        c = conn()
        clock = iter([100.0, 101.0])
        m = oa.OutputActivityMonitor(lambda: c, reader=FakeReader({10, 20}), exclude=lambda: {10},
                                     clock=lambda: next(clock), name_of=lambda pid: "Safari")
        self.assertEqual(m.sample(c), (True, {20}))
        m.reader = FakeReader({10})
        self.assertEqual(m.sample(c), (False, set()))
        self.assertEqual(c.execute("SELECT active, pids FROM output_activity ORDER BY sampled_at").fetchall(),
                         [(1, "20"), (0, "")])

    def test_system_alert_sounds_are_not_media(self):
        c = conn()
        m = oa.OutputActivityMonitor(lambda: c, reader=FakeReader({30, 40}), exclude=set, clock=lambda: 5.0,
                                     name_of={30: "systemsoundserverd", 40: "Music"}.get)
        self.assertEqual(m.sample(c), (True, {40}))
        m.reader = FakeReader({30})
        self.assertEqual(m.sample(c), (False, set()))

    def test_descendants_of_ramble_are_excluded(self):
        self.assertIn(__import__("os").getpid(), oa.ramble_pids())

    def test_active_fraction(self):
        c = conn()
        for t in range(100, 111):
            c.execute("INSERT INTO output_activity VALUES (?,?, '')", (float(t), int(t >= 105)))
        self.assertAlmostEqual(oa.active_fraction(c, 105, 109), 6 / 7)
        self.assertEqual(oa.active_fraction(c, 100, 103), 0.0)

    def test_unmeasured_intervals_fail_open(self):
        c = conn()
        self.assertIsNone(oa.active_fraction(c, 100, 105))
        bare = sqlite3.connect(":memory:")
        self.assertIsNone(oa.active_fraction(bare, 100, 105))  # monitor never ran
        for t in (100.0, 101.0, 112.0):
            c.execute("INSERT INTO output_activity VALUES (?,1,'')", (t,))
        self.assertIsNone(oa.active_fraction(c, 100, 112))  # 11 s gap

    @unittest.skipUnless(sys.platform == "darwin", "Core Audio only")
    def test_core_audio_read_is_available(self):
        self.assertIsInstance(oa.CoreAudioReader().output_pids(), set)


if __name__ == "__main__":
    unittest.main()
