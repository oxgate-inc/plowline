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


class OverlapTest(unittest.TestCase):
    def test_overlaps(self):
        self.assertTrue(paths_overlap("src/", "src/app.py"))
        self.assertTrue(paths_overlap("./docs/a.md", "docs/a.md"))
        self.assertTrue(paths_overlap("src/*.py", "src/app.py"))
        self.assertFalse(paths_overlap("src/app.py", "src/api.py"))
        self.assertFalse(paths_overlap(".github/ci.yml", "github/ci.yml"))


if __name__ == "__main__":
    unittest.main()
