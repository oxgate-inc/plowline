"""Lane rules: scope, --after, priority and the cap, dirty worktrees, notices and repair, briefs, follow-ups, clean."""
import shutil
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tests.helpers import PY, RepoCase  # noqa: E402


class ScopeTest(RepoCase):
    def test_files_outside_the_claim_are_a_warning(self):
        self.configure()
        done = self.lane("a", {"a.txt": "a\n", "other.txt": "x\n"}, "--paths", "a.txt")
        self.assertIn("1 file(s) outside the claim", done.stdout)
        self.assertIn("other.txt", done.stdout)
        self.assertEqual(self.state("a"), "candidate")

    def test_strict_paths_bounces_with_the_files_named(self):
        self.configure("strict_paths = true\n")
        self.pl("new", "a", "--paths", "src/")
        self.commit("a", {"src/a.txt": "a\n", "docs/x.txt": "x\n"})
        done = self.pl("done", "a")
        self.assertEqual(done.returncode, 1, done.stdout + done.stderr)
        self.assertEqual(self.state("a"), "bounced")
        notice = (self.app / ".plowline" / "notices" / "a.md").read_text()
        self.assertIn("OUT_OF_SCOPE", notice)
        self.assertIn("## Files outside the claim\n- docs/x.txt", notice)
        self.assertNotIn("src/a.txt", notice.split("## Files outside the claim")[1])

    def test_scope_is_measured_from_the_merge_base_with_the_trunk(self):
        self.configure("strict_paths = true\n")
        self.lane("a", {"a.txt": "a\n"}, "--paths", "a.txt")
        self.pl("new", "b", "--paths", "b.txt")
        self.assertIn("landed 1", self.pl("train").stdout)
        self.git("merge", "-q", "main", cwd=self.wt("b"))  # b picks up a.txt from the trunk
        self.commit("b", {"b.txt": "b\n"})
        done = self.pl("done", "b")
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertNotIn("outside the claim", done.stdout)

    def test_overlaps_are_advisory_and_shown_by_show(self):
        self.configure()
        self.pl("new", "a", "--paths", "src/")
        new = self.pl("new", "b", "--paths", "src/b.txt")
        self.assertEqual(new.returncode, 0)
        self.assertIn("overlap", new.stdout)
        self.assertIn("overlap:  src/b.txt ~ src/ in lane a", self.pl("show", "b").stdout)


class AfterAndPriorityTest(RepoCase):
    def test_after_waits_for_the_named_lane_to_land(self):
        self.configure()
        self.pl("new", "a")
        self.lane("b", {"b.txt": "b\n"}, "--after", "a")
        self.assertIn("after a (waiting)", self.pl("status").stdout)
        train = self.pl("train")
        self.assertIn("b waits for a to land", train.stdout)
        self.assertEqual(self.state("b"), "candidate")
        self.commit("a", {"a.txt": "a\n"})
        self.pl("done", "a")
        self.pl("train")  # a lands; b still waited during this train's selection
        self.assertEqual(self.state("a"), "landed")
        self.pl("train")
        self.assertEqual(self.state("b"), "landed")

    def test_watch_runs_no_train_for_a_lane_that_only_waits(self):
        self.configure()
        self.pl("new", "a")
        self.lane("b", {"b.txt": "b\n"}, "--after", "a")
        watched = self.pl("watch", "--once")
        self.assertNotIn("recent trains", watched.stdout)
        self.assertIn("after a (waiting)", watched.stdout)

    def test_after_must_name_a_lane(self):
        self.configure()
        new = self.pl("new", "b", "--after", "nope")
        self.assertEqual(new.returncode, 2)
        self.assertIn("no lane named 'nope'", new.stderr)

    def test_higher_priority_merges_first(self):
        self.configure()
        self.lane("low", {"low.txt": "x\n"})
        self.lane("high", {"high.txt": "x\n"}, "--priority", "5")
        self.assertIn("priority 5", self.pl("status").stdout)
        self.pl("train")
        log = self.git("log", "--first-parent", "--format=%s", "main").splitlines()
        self.assertEqual(log[:2], ["plowline: land lane/low", "plowline: land lane/high"])

    def test_new_refuses_with_one_line_reasons(self):
        self.configure("max_lanes = 2\n")
        self.pl("new", "a")
        self.pl("new", "b")
        cap = self.pl("new", "c")
        self.assertEqual(cap.returncode, 2)
        self.assertIn("max_lanes = 2 reached (a, b)", cap.stderr)
        self.assertEqual(len(cap.stderr.strip().splitlines()), 1)
        self.git("branch", "lane/d")
        self.assertIn("branch lane/d already exists", self.pl("new", "d").stderr)
        self.wt("e").mkdir(parents=True)
        self.assertIn("already exists", self.pl("new", "e").stderr)

    def test_new_refuses_a_dirty_trunk(self):
        self.configure()
        (self.app / "shared.txt").write_text("edited, not committed\n")
        new = self.pl("new", "a")
        self.assertEqual(new.returncode, 2)
        self.assertIn("has uncommitted changes (1 file(s))", new.stderr)

    def test_a_cleaned_lane_name_is_not_reused(self):
        self.configure()
        self.lane("a", {"a.txt": "a\n"})
        self.pl("train")
        self.pl("clean")
        again = self.pl("new", "a")
        self.assertEqual(again.returncode, 2)
        self.assertIn("pick a fresh name", again.stderr)


class DirtyDoneTest(RepoCase):
    def test_done_lists_what_is_uncommitted_and_stops(self):
        self.configure()
        self.pl("new", "a")
        self.commit("a", {"a.txt": "a\n"})
        (self.wt("a") / "a.txt").write_text("changed\n")
        (self.wt("a") / "new dir").mkdir()
        (self.wt("a") / "new dir" / "loose.txt").write_text("x\n")
        done = self.pl("done", "a")
        self.assertEqual(done.returncode, 1)
        self.assertIn(" M a.txt", done.stdout)
        self.assertIn("loose.txt", done.stdout)
        self.assertEqual(self.state("a"), "active")


FAKE_AGENT = """import os, sys, pathlib
notice = sys.stdin.read()
log = pathlib.Path(os.environ['AGENT_LOG'])
with log.open('a') as f:
    f.write(f"{sys.argv[1]}|{sys.argv[2]}|{pathlib.Path(sys.argv[3]).name}|{'GATE_FAIL' in notice}\\n")
"""


class NoticeAndRepairTest(RepoCase):
    def test_resume_prints_the_notice(self):
        self.configure()
        self.lane("a", {"a.txt": "BROKEN\n"})
        self.pl("train")
        resumed = self.pl("resume", "a")
        self.assertIn("# Lane a bounced: GATE_FAIL", resumed.stdout)
        self.assertIn("## What to do", resumed.stdout)
        self.assertIn("COMMANDS RUN:", resumed.stdout)

    def test_a_bounce_relaunches_the_agent_up_to_the_cap(self):
        from plowline.demo import ENV
        agent_log = self.tmp / "agent.log"
        ENV["AGENT_LOG"] = str(agent_log)
        self.addCleanup(ENV.pop, "AGENT_LOG", None)
        (self.tmp / "agent.py").write_text(FAKE_AGENT)
        agent = (self.tmp / "agent.py").as_posix()
        self.configure(f"agent = '\"{PY}\" \"{agent}\" {{lane}} {{path}} {{notice}}'\nrepair_attempts = 1\n")
        self.lane("a", {"a.txt": "BROKEN\n"})
        train = self.pl("train")
        self.assertIn("auto-repair 1/1: started the agent in lane a", train.stdout, train.stdout + train.stderr)
        self.wait_for(agent_log)
        lane, path, notice, saw = agent_log.read_text().strip().split("|")
        self.assertEqual((lane, Path(path), notice, saw), ("a", self.wt("a"), "a.md", "True"))
        self.assertEqual(self.state("a"), "active")
        self.assertIn("repairs 1/1", self.pl("status").stdout)
        # The repair did not fix it: the next bounce stops and asks for a person.
        self.assertEqual(self.pl("done", "a").returncode, 0)
        train = self.pl("train")
        self.assertIn("split this lane or re-plan", train.stdout)
        self.assertEqual(self.state("a"), "bounced")
        self.assertEqual(len(agent_log.read_text().strip().splitlines()), 1)

    def test_without_an_agent_template_bounces_stay_manual(self):
        self.configure("agent = \"claude\"\n")
        self.lane("a", {"a.txt": "BROKEN\n"})
        train = self.pl("train")
        self.assertNotIn("auto-repair", train.stdout)
        self.assertEqual(self.state("a"), "bounced")


class BriefTest(RepoCase):
    def test_brief_scaffold_has_the_sections_and_is_read_live(self):
        self.configure()
        new = self.pl("new", "a", "--brief", "--paths", "src/a.txt")
        self.assertEqual(new.returncode, 0, new.stderr)
        brief = self.app / "briefs" / "a.md"
        text = brief.read_text()
        for section in ("## Goal", "## Files you may touch\n- src/a.txt", "## Do not touch", "## Acceptance",
                        "## Report", "COMMIT:", "COMMANDS RUN:", "RESULT:", "NOT VERIFIED:"):
            self.assertIn(section, text)
        brief.write_text(text.replace("One or two sentences: what is true when this lane is done.", "Ship a."))
        self.assertIn("Ship a.", self.pl("show", "a").stdout)

    def test_brief_from_a_file_still_works(self):
        self.configure()
        (self.tmp / "b.md").write_text("# do b\n")
        self.pl("new", "b", "--brief", str(self.tmp / "b.md"))
        self.assertIn("# do b", self.pl("show", "b").stdout)

    def test_prompts_ask_for_the_evidence_block(self):
        from plowline import cli
        for key in ("COMMIT", "COMMANDS RUN", "RESULT", "NOT VERIFIED"):
            self.assertIn(key, cli.LANE_PROMPT)
            self.assertIn(key + ":", cli.REVIEW_PROMPT)
        self.assertIn("## Follow-ups", cli.REVIEW_PROMPT)


class FollowupsTest(RepoCase):
    def test_non_blocking_findings_are_kept_and_counted(self):
        (self.app / "review.py").write_text(
            "print('GO\\n\\n## Blocking\\nnone\\n\\n## Follow-ups\\n- rename the helper\\n- add a docstring')\n")
        self.configure()
        toml = (self.app / "plowline.toml").read_text().replace('review = ""', f"review = '\"{PY}\" review.py'")
        (self.app / "plowline.toml").write_text(toml)
        self.git("commit", "-qam", "reviewer")
        done = self.lane("a", {"a.txt": "a\n"})
        self.assertIn("follow-ups: 2 non-blocking finding(s)", done.stdout)
        text = (self.app / ".plowline" / "followups.md").read_text()
        self.assertIn("## a at ", text)
        self.assertIn("- rename the helper", text)
        self.assertIn("follow-ups: 2 in", self.pl("status").stdout)


class CleanTest(RepoCase):
    def test_clean_backs_up_env_files_and_keeps_logs(self):
        self.configure()
        self.lane("a", {"a.txt": "a\n"})
        self.pl("train")
        (self.wt("a") / ".env.local").write_text("SECRET=1\n")
        (self.wt("a") / "cfg").mkdir()
        (self.wt("a") / "cfg" / ".env").write_text("X=2\n")
        clean = self.pl("clean")
        self.assertIn("cleaned a", clean.stdout, clean.stdout + clean.stderr)
        backups = self.app / ".plowline" / "backups" / "a"
        self.assertEqual((backups / ".env.local").read_text(), "SECRET=1\n")
        self.assertEqual((backups / "cfg" / ".env").read_text(), "X=2\n")
        self.assertFalse(self.wt("a").exists())
        self.assertTrue((self.app / ".plowline" / "logs" / "a.log").exists())

    def test_clean_refuses_a_dirty_worktree(self):
        self.configure()
        self.lane("a", {"a.txt": "a\n"})
        self.pl("train")
        (self.wt("a") / "a.txt").write_text("edited after landing\n")
        clean = self.pl("clean")
        self.assertIn("kept a", clean.stdout)
        self.assertIn(" M a.txt", clean.stdout)
        self.assertTrue(self.wt("a").exists())

    def test_a_missing_worktree_is_parked(self):
        self.configure()
        self.pl("new", "a")
        shutil.rmtree(self.wt("a"))
        status = self.pl("status")
        self.assertEqual(status.returncode, 0, status.stderr)
        self.assertEqual(status.stdout.split()[1], "parked")
        done = self.pl("done", "a")
        self.assertEqual(done.returncode, 2)
        self.assertIn("is parked", done.stderr)


if __name__ == "__main__":
    unittest.main()
