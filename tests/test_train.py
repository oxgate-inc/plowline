"""The train: bisection on a red gate, conflict previews, generated files, a GO bound to its head, gate lists."""
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tests.helpers import PY, RepoCase  # noqa: E402


class BisectionTrainTest(RepoCase):
    def test_one_red_lane_among_four_bounces_and_the_rest_land(self):
        self.configure()
        for name in "abcd":
            self.lane(name, {f"{name}.txt": "BROKEN\n" if name == "c" else f"lane {name}\n"})
        train = self.pl("train")
        self.assertIn("bisection: red c; green a, b, d", train.stdout, train.stdout + train.stderr)
        self.assertIn("landed 3 lane(s)", train.stdout)
        self.assertEqual({n: self.state(n) for n in "abcd"},
                         {"a": "landed", "b": "landed", "c": "bounced", "d": "landed"})
        notice = (self.app / ".plowline" / "notices" / "c.md").read_text()
        self.assertIn("GATE_FAIL", notice)
        self.assertIn("broken files ['c.txt']", notice)
        self.assertIn("GATE: FAIL", notice)
        self.assertIn("a, b", notice)  # the green set it was blamed on top of

    def test_two_lanes_first_red_lands_the_second(self):
        self.configure()
        self.lane("a", {"a.txt": "BROKEN\n"})
        self.lane("b", {"b.txt": "fine\n"})
        train = self.pl("train")
        self.assertIn("landed 1 lane(s)", train.stdout, train.stdout + train.stderr)
        self.assertEqual((self.state("a"), self.state("b")), ("bounced", "landed"))
        self.assertIn("no lanes (the trunk alone)", train.stdout)  # a was blamed against the bare trunk

    def test_two_lanes_drop_last_is_the_first_probe(self):
        self.configure()
        self.lane("a", {"a.txt": "fine\n"})
        self.lane("b", {"b.txt": "BROKEN\n"})
        train = self.pl("train")
        gates = [ln for ln in train.stdout.splitlines() if ln.startswith("gate on")]
        self.assertEqual([g.split(" -> ")[0] for g in gates], ["gate on 2 lane(s): a, b", "gate on 1 lane(s): a"])
        self.assertEqual((self.state("a"), self.state("b")), ("landed", "bounced"))

    def test_a_red_trunk_blames_no_lane(self):
        (self.app / "trunk.txt").write_text("BROKEN\n")
        self.configure()
        self.lane("a", {"a.txt": "fine\n"})
        train = self.pl("train")
        self.assertEqual(train.returncode, 2)
        self.assertIn("GATE_RED_ON_TRUNK", train.stderr)
        self.assertEqual(self.state("a"), "candidate")

    def test_a_gate_that_cannot_run_blames_no_lane(self):
        self.configure(gate="gate = 'plowline-no-such-gate-command'")
        self.lane("a", {"a.txt": "fine\n"})
        train = self.pl("train")
        self.assertEqual(train.returncode, 2)
        self.assertIn("GATE: UNAVAILABLE", train.stderr)
        self.assertEqual(self.state("a"), "candidate")

    def test_gate_list_stops_at_the_first_red_step(self):
        marker = self.tmp / "second-step-ran"
        (self.app / "second.py").write_text(  # records the lane set each run of step 2 saw
            f"import os\nwith open({str(marker)!r}, 'a') as f:\n    f.write(os.environ.get('PLOWLINE_LANES', '') + '|')\n")
        self.configure(gate=f"gate = ['\"{PY}\" gate.py', '\"{PY}\" second.py']")
        self.lane("a", {"a.txt": "BROKEN\n"})
        train = self.pl("train")
        self.assertIn("GATE: FAIL at step 1/2", train.stdout, train.stdout + train.stderr)
        self.assertEqual(self.state("a"), "bounced")
        # step 2 never ran on lane a's red tree; it ran once, on the trunk alone, when a was blamed
        self.assertEqual(marker.read_text(), "|")
        self.lane("b", {"b.txt": "fine\n"})
        train = self.pl("train")
        self.assertIn("GATE: PASS (2 steps)", train.stdout, train.stdout + train.stderr)
        self.assertEqual(marker.read_text(), "|b|")


PAIR_GATE = ("import pathlib, sys\n"
             "sys.exit('x and y clash' if pathlib.Path('x.txt').exists() and pathlib.Path('y.txt').exists() else 0)\n")


class BisectionEdgeTest(RepoCase):
    def test_two_individually_red_lanes_both_bounce_and_the_rest_land(self):
        self.configure()
        for name in "abcd":
            self.lane(name, {f"{name}.txt": "BROKEN\n" if name in "bd" else f"{name}\n"})
        train = self.pl("train")
        self.assertIn("bisection: red b, d; green a, c", train.stdout, train.stdout + train.stderr)
        self.assertEqual({n: self.state(n) for n in "abcd"},
                         {"a": "landed", "b": "bounced", "c": "landed", "d": "bounced"})

    def test_a_pair_green_alone_but_red_together_blames_the_later_lane(self):
        (self.app / "pair.py").write_text(PAIR_GATE)
        self.configure(gate=f"gate = '\"{PY}\" pair.py'")
        self.lane("x", {"x.txt": "x\n"})
        self.lane("y", {"y.txt": "y\n"})
        train = self.pl("train")
        self.assertIn("bisection: red y; green x", train.stdout, train.stdout + train.stderr)
        self.assertEqual((self.state("x"), self.state("y")), ("landed", "bounced"))
        self.assertIn("x and y clash", (self.app / ".plowline" / "notices" / "y.md").read_text())

    def test_a_lane_that_no_longer_merges_in_a_smaller_set_bounces_as_a_conflict(self):
        from plowline import cli
        self.configure()
        self.lane("a", {"a.txt": "a\n"})
        self.lane("b", {"b.txt": "BROKEN\n"})
        self.lane("c", {"c.txt": "c\n"})
        real = cli.merge_lane

        def merge_lane(y, tree, lane):
            # c merges only on top of b, as if it built on b's change: without b it conflicts in c.txt.
            if lane.name == "c" and "land lane/b" not in cli.git("log", "--format=%s", cwd=tree):
                return False, ["c.txt"]
            return real(y, tree, lane)
        with mock.patch.object(cli, "merge_lane", merge_lane):
            code, out = self.in_process("train")
        self.assertEqual({n: self.state(n) for n in "abc"}, {"a": "landed", "b": "bounced", "c": "bounced"}, out)
        notice_c = (self.app / ".plowline" / "notices" / "c.md").read_text()
        self.assertIn("MERGE_CONFLICT", notice_c)
        self.assertIn("## Conflicting paths\n- c.txt", notice_c)
        self.assertNotIn("GATE_FAIL", notice_c)
        self.assertIn("GATE_FAIL", (self.app / ".plowline" / "notices" / "b.md").read_text())


class OldStateTest(RepoCase):
    def test_a_lane_saved_by_0_1_still_trains(self):
        import json
        self.configure()
        self.pl("new", "old")
        self.commit("old", {"old.txt": "old\n"})
        head = self.git("rev-parse", "lane/old")
        f = self.app / ".git" / "plowline" / "lanes" / "old.json"
        v01 = {k: v for k, v in json.loads(f.read_text()).items()
               if k in ("name", "branch", "worktree", "base", "paths", "brief", "state", "head", "note",
                        "queued_at", "updated_at")}
        v01.update(state="candidate", head=head, queued_at=1.0)
        f.write_text(json.dumps(v01))
        status = self.pl("status")
        self.assertEqual(status.returncode, 0, status.stderr)
        self.assertIn("old", status.stdout)
        self.assertEqual(self.pl("show", "old").returncode, 0)
        train = self.pl("train")
        self.assertIn("landed 1 lane(s)", train.stdout, train.stdout + train.stderr)
        self.assertEqual(self.state("old"), "landed")


class ConflictTrainTest(RepoCase):
    def check_conflict_bounce(self, train_output: str) -> None:
        self.assertEqual((self.state("a"), self.state("b")), ("landed", "bounced"), train_output)
        notice = (self.app / ".plowline" / "notices" / "b.md").read_text()
        self.assertIn("MERGE_CONFLICT", notice)
        self.assertIn("## Conflicting paths\n- shared.txt", notice)
        self.assertIn("git merge main", notice)
        self.assertIn("Never rebase", notice)

    def test_preview_bounces_a_conflict_with_its_paths(self):
        self.configure()
        self.lane("a", {"shared.txt": "by a\n"})
        self.lane("b", {"shared.txt": "by b\n", "b.txt": "b\n"})
        train = self.pl("train")
        self.check_conflict_bounce(train.stdout + train.stderr)

    def test_preview_never_runs_a_merge_for_a_conflicting_lane(self):
        from plowline import cli
        self.configure()
        self.lane("a", {"shared.txt": "by a\n"})
        self.lane("b", {"shared.txt": "by b\n"})
        calls = []
        real = cli.git_run

        def spy(*args, cwd):
            calls.append(args)
            return real(*args, cwd=cwd)
        with mock.patch.object(cli, "git_run", spy):
            code, out = self.in_process("train")
        self.check_conflict_bounce(out)
        merges = [c for c in calls if c[0] == "merge"]
        self.assertEqual(len(merges), 1, merges)  # only lane a was merged; b was refused by the preview
        self.assertTrue(any(c[0] == "merge-tree" for c in calls))

    def test_old_git_falls_back_to_a_merge_attempt(self):
        from plowline import cli
        self.configure()
        self.lane("a", {"shared.txt": "by a\n"})
        self.lane("b", {"shared.txt": "by b\n"})
        with mock.patch.object(cli.Yard, "git_version", return_value=(2, 37)):
            code, out = self.in_process("train")
        self.check_conflict_bounce(out)


REGEN = ("import pathlib\n"
         "names = sorted(p.name for p in pathlib.Path('.').glob('*.txt') if p.name != 'lock.txt')\n"
         "pathlib.Path('lock.txt').write_text('\\n'.join(names) + '\\n')\n")


class GeneratedConflictTest(RepoCase):
    def setUp(self):
        super().setUp()
        (self.app / "regen.py").write_text(REGEN)
        (self.app / "lock.txt").write_text("shared.txt\n")

    def test_a_conflict_only_in_generated_files_takes_the_trunk_and_regenerates(self):
        self.configure(f"generated = [\"lock.txt\"]\nregenerate = '\"{PY}\" regen.py'\n")
        self.lane("a", {"a.txt": "a\n", "lock.txt": "a.txt\nshared.txt\n"})
        self.lane("b", {"b.txt": "b\n", "lock.txt": "b.txt\nshared.txt\n"})
        train = self.pl("train")
        self.assertIn("landed 2 lane(s)", train.stdout, train.stdout + train.stderr)
        self.assertIn("generated conflict in lock.txt (b)", train.stdout)
        self.assertEqual(self.trunk_file("lock.txt").splitlines(), ["a.txt", "b.txt", "shared.txt"])

    def test_a_conflict_that_also_touches_a_real_file_still_bounces(self):
        self.configure(f"generated = [\"lock.txt\"]\nregenerate = '\"{PY}\" regen.py'\n")
        self.lane("a", {"shared.txt": "by a\n", "lock.txt": "a\n"})
        self.lane("b", {"shared.txt": "by b\n", "lock.txt": "b\n"})
        self.pl("train")
        self.assertEqual(self.state("b"), "bounced")
        notice = (self.app / ".plowline" / "notices" / "b.md").read_text()
        self.assertIn("- shared.txt", notice)
        self.assertIn("- lock.txt", notice)

    def test_generated_without_regenerate_bounces_with_a_hint(self):
        self.configure("generated = [\"lock.txt\"]\n")
        self.lane("a", {"lock.txt": "a\n"})
        self.lane("b", {"lock.txt": "b\n"})
        self.pl("train")
        self.assertEqual(self.state("b"), "bounced")
        self.assertIn("set `regenerate`", self.lane_json("b")["note"])


class BoundReviewTest(RepoCase):
    def test_a_go_is_bound_to_the_head_it_reviewed(self):
        self.configure()
        self.lane("a", {"a.txt": "one\n"})
        info = self.lane_json("a")
        self.assertEqual(info["reviewed_head"], self.git("rev-parse", "lane/a"))
        self.assertEqual(info["reviewed_trunk"], self.git("rev-parse", "main"))
        self.commit("a", {"a.txt": "BROKEN\n"}, "after the GO")
        train = self.pl("train")
        self.assertIn("STALE_REVIEW", train.stdout, train.stdout + train.stderr)
        self.assertIn("no candidates", train.stdout)
        self.assertEqual(self.state("a"), "active")
        self.assertNotIn("one", self.git("ls-tree", "--name-only", "main"))

    def test_a_moved_trunk_is_reported_and_the_train_still_gates(self):
        self.configure()
        self.lane("a", {"a.txt": "a\n"})
        (self.app / "trunk.txt").write_text("moved\n")
        self.git("add", "-A")
        self.git("commit", "-qm", "trunk moves")
        train = self.pl("train")
        self.assertIn("moved since its review", train.stdout)
        self.assertIn("landed 1 lane(s)", train.stdout)

    def test_a_reviewer_that_exits_zero_but_says_no_go_is_a_no_go(self):
        (self.app / "review.py").write_text("print('NO\\u2011GO: the change has no test')\n")
        self.configure()
        toml = (self.app / "plowline.toml").read_text().replace('review = ""', f"review = '\"{PY}\" review.py'")
        (self.app / "plowline.toml").write_text(toml)
        self.git("commit", "-qam", "reviewer")
        self.pl("new", "a")
        self.commit("a", {"a.txt": "a\n"})
        done = self.pl("done", "a")
        self.assertEqual(done.returncode, 1, done.stdout)
        self.assertIn("NO-GO for a", done.stdout)
        self.assertEqual(self.state("a"), "active")


if __name__ == "__main__":
    unittest.main()
