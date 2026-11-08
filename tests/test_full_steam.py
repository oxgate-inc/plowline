"""`pl train --full-steam`: a train whenever lanes are done, one line per cycle, until Ctrl-C."""
import os
import re
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tests.helpers import RepoCase  # noqa: E402

LINE = re.compile(r"^\d\d:\d\d:\d\d  ")


def stop_after(n: int):
    """A time.sleep stand-in that presses Ctrl-C on the n-th sleep."""
    calls = [0]

    def sleep(_seconds):
        calls[0] += 1
        if calls[0] >= n:
            raise KeyboardInterrupt
    return sleep


class FullSteamTest(RepoCase):
    def test_status_line_per_cycle(self):
        self.configure()
        self.lane("a", {"a.txt": "a\n"})
        self.lane("b", {"b.txt": "BROKEN\n"})
        self.pl("new", "c")
        with mock.patch("plowline.cli.time.sleep", stop_after(1)):
            code, out = self.in_process("train", "--full-steam")
        self.assertEqual(code, 0, out)
        lines = [ln for ln in out.splitlines() if LINE.match(ln)]
        self.assertEqual(len(lines), 1, out)
        self.assertRegex(lines[0], r"^\d\d:\d\d:\d\d  1 landed, 1 bounced \(lane/b: gate red\), 1 working, "
                                   r"next check in 30s$")
        self.assertNotIn("gate on", out)  # the train's own output goes to the log
        log = (self.app / ".plowline" / "logs" / "train.log").read_text()
        self.assertIn("bisection: red b", log)
        self.assertEqual((self.state("a"), self.state("b")), ("landed", "bounced"))

    def test_idle_prints_nothing_and_ctrl_c_between_cycles_exits_cleanly(self):
        self.configure()
        self.pl("new", "a")
        with mock.patch("plowline.cli.time.sleep", stop_after(3)):
            code, out = self.in_process("train", "--watch", "--every", "5")
        self.assertEqual(code, 0, out)
        self.assertEqual([ln for ln in out.splitlines() if LINE.match(ln)], [])
        self.assertIn("full steam stopped", out)
        self.assertFalse((self.app / ".plowline" / "full-steam.lock").exists())

    def test_until_empty_exits_once_every_lane_has_landed(self):
        self.configure()
        self.lane("a", {"a.txt": "a\n"})
        self.lane("b", {"b.txt": "b\n"})
        started = time.monotonic()
        run = self.pl("train", "--full-steam", "--until-empty", "--every", "1")
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        self.assertLess(time.monotonic() - started, 60)
        self.assertIn("2 landed, 0 bounced, 0 working", run.stdout)
        self.assertIn("no lane is working, done or bounced", run.stdout)

    def test_until_empty_waits_for_a_bounced_lane(self):
        self.configure()
        self.lane("a", {"a.txt": "BROKEN\n"})
        with mock.patch("plowline.cli.time.sleep", stop_after(2)):
            code, out = self.in_process("train", "--full-steam", "--until-empty")
        self.assertNotIn("no lane is working", out)
        self.assertIn("full steam stopped", out)

    def test_a_second_loop_is_refused(self):
        self.configure()
        lock = self.app / ".plowline" / "full-steam.lock"
        lock.parent.mkdir(parents=True, exist_ok=True)
        lock.write_text("12345")
        run = self.pl("train", "--full-steam", "--until-empty")
        self.assertEqual(run.returncode, 2)
        self.assertIn("another full-steam loop holds", run.stderr)

    def test_a_held_train_lock_skips_the_cycle_without_counting_a_failure(self):
        self.configure()
        self.lane("a", {"a.txt": "a\n"})
        lock = self.app / ".plowline" / "train.lock"
        lock.write_text("12345")
        with mock.patch("plowline.cli.time.sleep", stop_after(6)):
            code, out = self.in_process("train", "--full-steam")
        self.assertEqual(code, 0, out)
        self.assertIn("another train holds", out)
        self.assertNotIn("failed", out)
        self.assertEqual(self.state("a"), "candidate")

    def test_a_stale_lock_is_reported_and_removed(self):
        self.configure()
        self.lane("a", {"a.txt": "a\n"})
        lock = self.app / ".plowline" / "train.lock"
        lock.write_text("12345")
        old = time.time() - 120
        os.utime(lock, (old, old))
        run = self.pl("train", "--full-steam", "--until-empty", "--every", "1", "--cycle-timeout", "60")
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        self.assertIn("removed a stale train lock", run.stdout)
        self.assertEqual(self.state("a"), "landed")

    def test_five_failed_cycles_in_a_row_stop_the_loop(self):
        from plowline import cli
        self.configure()
        self.lane("a", {"a.txt": "a\n"})
        with mock.patch("plowline.cli.time.sleep", lambda _s: None), \
                mock.patch.object(cli, "run_train", side_effect=RuntimeError("boiler burst")):
            code, out = self.in_process("train", "--full-steam")
        self.assertEqual(code, 2, out)
        failed = [ln for ln in out.splitlines() if "cycle failed" in ln]
        self.assertEqual(len(failed), 5, out)
        self.assertIn("(5/5)", failed[-1])
        self.assertIn("boiler burst", out.splitlines()[-1])
        log = (self.app / ".plowline" / "logs" / "train.log").read_text()
        self.assertEqual(log.count("RuntimeError: boiler burst"), 5)

    def test_a_good_cycle_resets_the_failure_count(self):
        from plowline import cli
        self.configure()
        self.lane("a", {"a.txt": "a\n"})
        results = iter([RuntimeError("one"), RuntimeError("two"), 0] + [RuntimeError("x")] * 5)

        def flaky(*_a, **_k):
            r = next(results)
            if isinstance(r, Exception):
                raise r
            return r
        with mock.patch("plowline.cli.time.sleep", lambda _s: None), \
                mock.patch.object(cli, "run_train", side_effect=flaky):
            code, out = self.in_process("train", "--full-steam")
        self.assertEqual(code, 2, out)
        self.assertEqual(len([ln for ln in out.splitlines() if "cycle failed" in ln]), 7)


if __name__ == "__main__":
    unittest.main()
