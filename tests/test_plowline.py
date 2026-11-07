import os
import shutil
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from examples import demo  # noqa: E402
from plowline.cli import paths_overlap  # noqa: E402


class TrainTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.result = demo.run(say=lambda _: None)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.result["tmp"], ignore_errors=True)

    def test_clean_lanes_land_and_conflict_lands_after_resume(self):
        log = self.result["log"]
        for name in "abcd":
            self.assertIn(f"plowline: land lane/{name}", log)
        self.assertNotIn("lane/e", log)

    def test_red_lane_stays_bounced_and_landed_lanes_are_cleaned(self):
        self.assertEqual(self.result["states"], {"e": "bounced"})

    def test_resolved_conflict_keeps_both_changes(self):
        self.assertEqual(self.result["shared"].splitlines(), ["changed by c", "changed by d"])


class ReviewTest(unittest.TestCase):
    def test_custom_reviewer_exit_code_decides(self):
        import subprocess
        import tempfile
        tmp = Path(tempfile.mkdtemp(prefix="plowline-review-"))
        try:
            origin, app = tmp / "origin.git", tmp / "app"
            sh = lambda *a, cwd=app: subprocess.run(a, cwd=cwd, env=demo.ENV, capture_output=True, text=True)
            sh("git", "init", "-q", "--bare", "-b", "main", str(origin), cwd=tmp)
            sh("git", "clone", "-q", str(origin), str(app), cwd=tmp)
            sh("git", "checkout", "-q", "-b", "main")
            (app / "review.py").write_text("import os, sys\nsys.exit(os.environ['PLOWLINE_LANE'] == 'bad')\n")
            (app / "plowline.toml").write_text(f"[plowline]\nreview = '\"{sys.executable}\" review.py'\n")
            sh("git", "add", "-A")
            sh("git", "commit", "-qm", "init")
            sh("git", "push", "-q", "-u", "origin", "main")
            codes = {}
            for name in ("good", "bad"):
                sh(sys.executable, "-m", "plowline", "new", name)
                wt = tmp / "app-lanes" / name
                (wt / f"{name}.txt").write_text(name)
                sh("git", "add", "-A", cwd=wt)
                sh("git", "commit", "-qm", name, cwd=wt)
                codes[name] = sh(sys.executable, "-m", "plowline", "done", name).returncode
            self.assertEqual(codes, {"good": 0, "bad": 1})
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class SetupWizardTest(unittest.TestCase):
    def test_wizard_answers_land_in_config_and_agent_files(self):
        import os
        import subprocess
        import tempfile
        from unittest import mock

        from plowline import cli
        tmp = Path(tempfile.mkdtemp(prefix="plowline-setup-"))
        cwd = os.getcwd()
        try:
            app = tmp / "app"
            run = lambda *a, cwd=app: subprocess.run(a, cwd=cwd, env=demo.ENV, capture_output=True, text=True)
            run("git", "init", "-q", "--bare", "-b", "main", str(tmp / "origin.git"), cwd=tmp)
            run("git", "clone", "-q", str(tmp / "origin.git"), str(app), cwd=tmp)
            (app / "AGENTS.md").write_text("# Existing rules\n")
            os.chdir(app)
            # remote, trunk, gate, review ('-' = none), agent, regenerate, commit?, push?
            answers = iter(["", "", f'"{sys.executable}" -c "pass"', "-", "", "-", "n", "n"])
            with mock.patch.object(sys.stdin, "isatty", return_value=True), \
                    mock.patch("builtins.input", lambda _: next(answers)), \
                    mock.patch.dict(os.environ, {k: v for k, v in demo.ENV.items() if k.startswith("GIT_")}):
                self.assertEqual(cli.main(["setup"]), 0)
            cfg = (app / "plowline.toml").read_text()
            self.assertIn('review = ""', cfg)
            self.assertIn(sys.executable.replace("\\", "\\\\"), cfg)
            agents = (app / "AGENTS.md").read_text()
            self.assertTrue(agents.startswith("# Existing rules"))
            self.assertIn(cli.AGENT_RULES_HEADING, agents)
            self.assertTrue((app / "CLAUDE.md").exists() and (app / "briefs" / "TEMPLATE.md").exists())
            skill = (app / ".claude" / "skills" / "plowline" / "SKILL.md").read_text()
            self.assertTrue(skill.startswith("---\nname: plowline\n"))
            self.assertIn(".claude/skills/plowline/SKILL.md", agents)
        finally:
            os.chdir(cwd)
            shutil.rmtree(tmp, ignore_errors=True)


class SoloLocalTest(unittest.TestCase):
    """One person, no remote: setup, two lanes, open --print, watch --once lands them on the local trunk."""

    def test_local_only_lanes_land_through_watch(self):
        import subprocess
        import tempfile
        tmp = Path(tempfile.mkdtemp(prefix="plowline-solo-")).resolve()
        try:
            app = tmp / "app"
            app.mkdir()
            run = lambda *a, cwd=app: subprocess.run(a, cwd=cwd, env=demo.ENV, capture_output=True, text=True)
            pl = lambda *a, cwd=app: run(sys.executable, "-m", "plowline", *a, cwd=cwd)
            run("git", "init", "-q", "-b", "main")
            (app / "README.md").write_text("solo")
            run("git", "add", "-A")
            run("git", "commit", "-qm", "init")
            setup = pl("setup", "--yes", "--commit", "--gate", f'"{sys.executable}" -c "pass"', "--review", "")
            self.assertEqual(setup.returncode, 0, setup.stderr)
            self.assertIn("local-only mode", setup.stdout)
            for name in ("one", "two"):
                self.assertEqual(pl("new", name, "--paths", f"{name}.txt").returncode, 0)
                wt = tmp / "app-lanes" / name
                (wt / f"{name}.txt").write_text(name)
                run("git", "add", "-A", cwd=wt)
                run("git", "commit", "-qm", name, cwd=wt)
            opened = pl("open", "one", "--print")
            self.assertIn(str(tmp / "app-lanes" / "one"), opened.stdout)
            self.assertIn("pl done one", opened.stdout)
            for name in ("one", "two"):
                self.assertEqual(pl("done", name, cwd=tmp / "app-lanes" / name).returncode, 0)
            watched = pl("watch", "--once")
            self.assertIn("landed 2 lane(s)", watched.stdout)
            self.assertTrue((app / "one.txt").exists() and (app / "two.txt").exists())
            self.assertEqual(run("git", "status", "--porcelain").stdout, "")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class LandModesTest(unittest.TestCase):
    """on_land runs after each landing; land = "pr" opens one PR per train and waits for it to merge."""

    def setUp(self):
        import subprocess
        import tempfile
        self.tmp = Path(tempfile.mkdtemp(prefix="plowline-land-")).resolve()
        self.app = self.tmp / "app"
        self.run_ = lambda *a, cwd=self.app: subprocess.run(a, cwd=cwd, env=demo.ENV, capture_output=True, text=True)
        self.pl = lambda *a, cwd=self.app: self.run_(sys.executable, "-m", "plowline", *a, cwd=cwd)
        self.run_("git", "init", "-q", "--bare", "-b", "main", str(self.tmp / "origin.git"), cwd=self.tmp)
        self.run_("git", "clone", "-q", str(self.tmp / "origin.git"), str(self.app), cwd=self.tmp)
        self.run_("git", "checkout", "-q", "-b", "main")
        (self.app / "hook.py").write_text(
            "import os, pathlib\n"
            "keys = ('PLOWLINE_LANES', 'PLOWLINE_COMMIT', 'PLOWLINE_PR')\n"
            "with pathlib.Path(os.environ['HOOK_LOG']).open('a') as f:\n"
            "    f.write('|'.join(os.environ[k] for k in keys) + '\\n')\n")
        self.hook_log = self.tmp / "hook.log"
        demo.ENV["HOOK_LOG"] = str(self.hook_log)

    def tearDown(self):
        demo.ENV.pop("HOOK_LOG", None)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def configure(self, extra: str) -> None:
        py = Path(sys.executable).as_posix()
        (self.app / "plowline.toml").write_text(
            f"[plowline]\ngate = '\"{py}\" -c \"pass\"'\non_land = '\"{py}\" hook.py'\n{extra}")
        self.run_("git", "add", "-A")
        self.run_("git", "commit", "-qm", "init")
        self.run_("git", "push", "-q", "-u", "origin", "main")

    def lane(self, name: str) -> None:
        self.assertEqual(self.pl("new", name).returncode, 0)
        wt = self.tmp / "app-lanes" / name
        (wt / f"{name}.txt").write_text(name)
        self.run_("git", "add", "-A", cwd=wt)
        self.run_("git", "commit", "-qm", name, cwd=wt)
        self.assertEqual(self.pl("done", name).returncode, 0)

    def state(self, name: str) -> str:
        return next(ln.split()[1] for ln in self.pl("status").stdout.splitlines() if ln.startswith(name))

    def test_on_land_gets_the_landed_lanes_and_commit(self):
        self.configure("")
        self.lane("one")
        train = self.pl("train")
        self.assertIn("on_land: ok", train.stdout)
        main = self.run_("git", "rev-parse", "origin/main").stdout.strip()
        self.assertEqual(self.hook_log.read_text().splitlines(), [f"one|{main}|"])

    def test_pr_mode_opens_a_pr_waits_and_lands_on_merge(self):
        import json
        gh = self.tmp / "fake_gh.py"
        gh.write_text(
            "import pathlib, sys\n"
            f"d = pathlib.Path({str(self.tmp)!r})\n"
            "args = sys.argv[1:]\n"
            "if args[:2] == ['pr', 'create']:\n"
            "    (d / 'pr-body.txt').write_text(sys.stdin.read())\n"
            "    print('https://example.test/pr/1')\n"
            "elif args[:2] == ['pr', 'view']:\n"
            "    print((d / 'pr-state.json').read_text())\n")
        self.configure(f"land = \"pr\"\ngh = '\"{Path(sys.executable).as_posix()}\" \"{gh.as_posix()}\"'\n")
        pr_state = self.tmp / "pr-state.json"

        self.lane("one")
        opened = self.pl("train")
        self.assertIn("opened train PR https://example.test/pr/1", opened.stdout, opened.stderr)
        self.assertEqual(self.state("one"), "proposed")
        self.assertIn("lane/one", (self.tmp / "pr-body.txt").read_text())
        branch = self.run_("git", "ls-remote", "--heads", "origin", "plowline/train-*").stdout.split()[1]

        pr_state.write_text(json.dumps({"state": "OPEN", "mergeCommit": None}))
        self.assertIn("waiting on train PR", self.pl("train").stdout)
        self.assertFalse(self.hook_log.exists())

        self.run_("git", "fetch", "-q", "origin")
        self.run_("git", "push", "-q", "origin", f"{branch}:refs/heads/main")
        merged = self.run_("git", "rev-parse", branch).stdout.strip()
        pr_state.write_text(json.dumps({"state": "MERGED", "mergeCommit": {"oid": merged}}))
        landed = self.pl("train")
        self.assertIn("train PR merged: landed 1 lane(s)", landed.stdout)
        self.assertEqual(self.state("one"), "landed")
        self.assertEqual(self.hook_log.read_text().splitlines(), [f"one|{merged}|https://example.test/pr/1"])

        self.lane("two")
        self.pl("train")  # same second as the last train: its branch name must still be new
        self.assertEqual(self.state("two"), "proposed")
        pr_state.write_text(json.dumps({"state": "CLOSED", "mergeCommit": None}))
        self.pl("train")
        self.assertEqual(self.state("two"), "bounced")

    def test_pr_mode_frees_lane_environments_on_merge_and_on_a_closed_pr(self):
        import json
        gh = self.tmp / "fake_gh.py"
        gh.write_text(
            "import pathlib, sys\n"
            f"d = pathlib.Path({str(self.tmp)!r})\n"
            "if sys.argv[1:3] == ['pr', 'create']:\n"
            "    sys.stdin.read()\n"
            "    print('https://example.test/pr/1')\n"
            "elif sys.argv[1:3] == ['pr', 'view']:\n"
            "    print((d / 'pr-state.json').read_text())\n")
        log = self.tmp / "stanchion.log"
        fake = self.tmp / "fake_stanchion.py"
        fake.write_text(f"import sys\nwith open({str(log)!r}, 'a') as f:\n"
                        "    f.write(' '.join(sys.argv[1:3]) + '\\n')\n")
        py = Path(sys.executable).as_posix()
        self.configure(f"land = \"pr\"\ngh = '\"{py}\" \"{gh.as_posix()}\"'\n\n"
                       f"[stanchion]\ncommand = '\"{py}\" \"{fake.as_posix()}\"'\n")
        pr_state = self.tmp / "pr-state.json"
        calls = lambda: log.read_text().splitlines()

        self.lane("one")
        self.pl("train")
        pr_state.write_text(json.dumps({"state": "OPEN", "mergeCommit": None}))
        self.pl("train")
        self.assertEqual(calls(), ["up one"])  # proposed and waiting: the lane keeps its environment

        branch = self.run_("git", "ls-remote", "--heads", "origin", "plowline/train-*").stdout.split()[1]
        self.run_("git", "push", "-q", "origin", f"{branch}:refs/heads/main")
        merged = self.run_("git", "rev-parse", branch).stdout.strip()
        pr_state.write_text(json.dumps({"state": "MERGED", "mergeCommit": {"oid": merged}}))
        self.assertIn("train PR merged", self.pl("train").stdout)
        self.assertEqual(calls()[1:], ["down one"])

        self.lane("two")
        self.pl("train")
        pr_state.write_text(json.dumps({"state": "CLOSED", "mergeCommit": None}))
        self.pl("train")
        self.assertEqual(self.state("two"), "bounced")
        self.assertEqual(calls()[2:], ["up two", "down two"])


class StanchionHookTest(unittest.TestCase):
    """With env = "stanchion", lanes get `stanchion up` when they open or resume and `stanchion down` when they land or bounce."""

    def setUp(self):
        import subprocess
        import tempfile
        self.tmp = Path(tempfile.mkdtemp(prefix="plowline-stanchion-")).resolve()
        self.app = self.tmp / "app"
        self.app.mkdir()
        self.run_ = lambda *a, cwd=self.app: subprocess.run(a, cwd=cwd, env=demo.ENV, capture_output=True, text=True)
        self.pl = lambda *a, cwd=self.app: self.run_(sys.executable, "-m", "plowline", *a, cwd=cwd)
        self.log = self.tmp / "stanchion.log"
        self.fake = self.tmp / "fake_stanchion.py"
        self.fake.write_text(f"import sys\nwith open({str(self.log)!r}, 'a') as f:\n"
                             "    f.write(' '.join(sys.argv[1:]) + '\\n')\nprint('stanchion ok')\n")
        self.run_("git", "init", "-q", "-b", "main")
        (self.app / "gate.py").write_text("import pathlib, sys\nsys.exit(pathlib.Path('red.txt').exists())\n")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def configure(self, stanchion: str) -> None:
        py = Path(sys.executable).as_posix()
        (self.app / "plowline.toml").write_text(
            f"[plowline]\nremote = \"\"\nreview = \"\"\ngate = '\"{py}\" gate.py'\n{stanchion}")
        self.run_("git", "add", "-A")
        self.run_("git", "commit", "-qm", "init")

    def lane(self, name: str) -> Path:
        new = self.pl("new", name)
        self.assertEqual(new.returncode, 0, new.stderr)
        wt = self.tmp / "app-lanes" / name
        (wt / f"{name}.txt").write_text(name)
        self.run_("git", "add", "-A", cwd=wt)
        self.run_("git", "commit", "-qm", name, cwd=wt)
        self.assertEqual(self.pl("done", name).returncode, 0)
        return wt

    def calls(self) -> list[str]:
        return self.log.read_text().splitlines() if self.log.exists() else []

    def test_lanes_get_up_on_open_and_down_on_land_and_bounce(self):
        py = Path(sys.executable).as_posix()
        self.configure(f"env = \"stanchion\"\n\n[stanchion]\ncommand = '\"{py}\" \"{self.fake.as_posix()}\"'\n")
        good, red = self.lane("good"), self.lane("red")
        self.assertEqual(self.calls(), [f"up good --path {good}", f"up red --path {red}"])
        train = self.pl("train")
        self.assertIn("landed 1 lane(s)", train.stdout)
        self.assertEqual(self.calls()[2:], ["down red", "down good"])
        resumed = self.pl("resume", "red")
        self.assertIn("stanchion ok", resumed.stdout)
        self.assertEqual(self.calls()[4:], [f"up red --path {red}"])

    def test_a_stanchion_section_alone_turns_it_on(self):
        py = Path(sys.executable).as_posix()
        self.configure(f"\n[stanchion]\ncommand = '\"{py}\" \"{self.fake.as_posix()}\"'\n")
        self.lane("one")
        self.assertEqual(self.calls()[0].split()[:2], ["up", "one"])

    def test_off_by_default(self):
        self.configure("")
        self.lane("one")
        self.assertEqual(self.calls(), [])

    def test_missing_stanchion_is_one_line_and_the_lane_still_opens(self):
        self.configure("env = \"stanchion\"\n\n[stanchion]\ncommand = \"stanchion-not-installed-here\"\n")
        new = self.pl("new", "one")
        self.assertEqual(new.returncode, 0, new.stderr)
        notices = [l for l in new.stdout.splitlines() if "not on PATH" in l]
        self.assertEqual(len(notices), 1)
        self.assertTrue((self.tmp / "app-lanes" / "one").is_dir())

    def test_a_failing_stanchion_does_not_stop_the_lane(self):
        failing = self.tmp / "failing_stanchion.py"
        failing.write_text("import sys\nprint('psql failed', file=sys.stderr)\nsys.exit(2)\n")
        py = Path(sys.executable).as_posix()
        self.configure(f"env = \"stanchion\"\n\n[stanchion]\ncommand = '\"{py}\" \"{failing.as_posix()}\"'\n")
        new = self.pl("new", "one")
        self.assertEqual(new.returncode, 0, new.stderr)
        self.assertIn("stanchion up one failed", new.stdout)
        self.assertIn("psql failed", new.stdout)

    def test_a_stanchion_that_hangs_times_out_and_the_lane_still_opens(self):
        import time
        slow = self.tmp / "slow_stanchion.py"
        slow.write_text("import time\ntime.sleep(30)\n")
        py = Path(sys.executable).as_posix()
        self.configure(f"\n[stanchion]\ncommand = '\"{py}\" \"{slow.as_posix()}\"'\ntimeout = 1\n")
        started = time.monotonic()
        new = self.pl("new", "one")
        self.assertLess(time.monotonic() - started, 20)
        self.assertEqual(new.returncode, 0, new.stderr)
        self.assertIn("stanchion up one failed (the lane carries on)", new.stdout)
        self.assertIn("timed out after 1 seconds", new.stdout)
        self.assertTrue((self.tmp / "app-lanes" / "one").is_dir())

    def test_env_off_wins_over_a_stanchion_table(self):
        py = Path(sys.executable).as_posix()
        self.configure(f"env = \"off\"\n\n[stanchion]\ncommand = '\"{py}\" \"{self.fake.as_posix()}\"'\n")
        new = self.pl("new", "one")
        self.assertEqual(new.returncode, 0, new.stderr)
        self.assertEqual(self.calls(), [])
        self.assertNotIn("stanchion ok", new.stdout)
        self.assertNotIn("not known", new.stdout)

    def test_an_unknown_env_value_is_reported(self):
        self.configure("env = \"stanchoin\"\n")
        new = self.pl("new", "one")
        self.assertEqual(new.returncode, 0, new.stderr)
        self.assertIn("env = 'stanchoin' is not known", new.stdout)
        self.assertEqual(self.calls(), [])

    @unittest.skipUnless(os.name == "nt", "Windows paths")
    def test_windows_paths_to_an_exe_and_a_cmd_shim(self):
        shim = self.tmp / "bin dir" / "stanchion.cmd"
        shim.parent.mkdir()
        shim.write_text(f'@"{sys.executable}" "{self.fake}" %*\r\n')
        # TOML literal strings keep backslashes exactly as a Windows user types them
        self.configure(f"\n[stanchion]\ncommand = '{sys.executable} \"{self.fake}\"'\n")
        self.assertEqual(self.pl("new", "one").returncode, 0)
        self.run_("git", "rm", "-q", "plowline.toml")
        self.configure(f"\n[stanchion]\ncommand = '\"{shim}\"'\n")
        two = self.pl("new", "two")
        self.assertEqual(two.returncode, 0, two.stderr)
        self.assertNotIn("not on PATH", two.stdout)
        self.assertEqual([c.split()[:2] for c in self.calls()], [["up", "one"], ["up", "two"]])

    def test_lane_env_reports_an_os_error_and_carries_on(self):
        import io
        from contextlib import redirect_stdout
        from unittest import mock
        from plowline import cli
        self.configure(f"\n[stanchion]\ncommand = '\"{Path(sys.executable).as_posix()}\"'\n")
        y = cli.Yard(self.app)
        lane = cli.Lane(name="one", branch="lane/one", worktree=str(self.app), base="main")
        out = io.StringIO()
        with mock.patch.object(cli.subprocess, "run", side_effect=OSError("exec format error")), redirect_stdout(out):
            y.lane_env("up", lane)
        self.assertIn("stanchion up one failed (the lane carries on)", out.getvalue())
        self.assertIn("exec format error", out.getvalue())


class CommandArgvTest(unittest.TestCase):
    """A Windows path in `command` keeps its backslashes; POSIX quoting still works elsewhere."""

    def test_windows_backslashes_survive(self):
        from unittest import mock
        from plowline import cli
        with mock.patch.object(cli.os, "name", "nt"):
            self.assertEqual(cli.command_argv(r"C:\Tools\stanchion.exe --quiet"),
                             [r"C:\Tools\stanchion.exe", "--quiet"])
            self.assertEqual(cli.command_argv(r'"C:\Program Files\pipx\stanchion.cmd" "C:\a b\x.py"'),
                             [r"C:\Program Files\pipx\stanchion.cmd", r"C:\a b\x.py"])

    def test_posix_quoting(self):
        from unittest import mock
        from plowline import cli
        with mock.patch.object(cli.os, "name", "posix"):
            self.assertEqual(cli.command_argv("'/opt/my tools/stanchion' -v"), ["/opt/my tools/stanchion", "-v"])


class OverlapTest(unittest.TestCase):
    def test_overlaps(self):
        self.assertTrue(paths_overlap("src/", "src/app.py"))
        self.assertTrue(paths_overlap("./docs/a.md", "docs/a.md"))
        self.assertTrue(paths_overlap("src/*.py", "src/app.py"))
        self.assertFalse(paths_overlap("src/app.py", "src/api.py"))
        self.assertFalse(paths_overlap(".github/ci.yml", "github/ci.yml"))


if __name__ == "__main__":
    unittest.main()
