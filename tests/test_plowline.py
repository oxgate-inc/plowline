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


class OverlapTest(unittest.TestCase):
    def test_overlaps(self):
        self.assertTrue(paths_overlap("src/", "src/app.py"))
        self.assertTrue(paths_overlap("./docs/a.md", "docs/a.md"))
        self.assertTrue(paths_overlap("src/*.py", "src/app.py"))
        self.assertFalse(paths_overlap("src/app.py", "src/api.py"))
        self.assertFalse(paths_overlap(".github/ci.yml", "github/ci.yml"))


if __name__ == "__main__":
    unittest.main()
