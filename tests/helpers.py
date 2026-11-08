"""A throwaway local-only repository with a gate, for train and lane tests."""
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from plowline.demo import ENV  # noqa: E402

PY = Path(sys.executable).as_posix()

# Red when any .txt file in the tree says BROKEN.
GATE = ("import pathlib, sys\n"
        "bad = sorted(p.name for p in pathlib.Path('.').rglob('*.txt') if 'BROKEN' in p.read_text())\n"
        "print('checked', len(list(pathlib.Path('.').rglob('*.txt'))), 'files')\n"
        "sys.exit(f'gate: broken files {bad}' if bad else 0)\n")


class RepoCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="plowline-t-")).resolve()
        self.app = self.tmp / "app"
        self.app.mkdir()
        self.git("init", "-q", "-b", "main")
        (self.app / "gate.py").write_text(GATE)
        (self.app / ".gitignore").write_text(".plowline/\n.env*\n")
        (self.app / "shared.txt").write_text("original\n")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_(self, *args, cwd=None, stdin=None):
        return subprocess.run(args, cwd=cwd or self.app, env=ENV, capture_output=True, text=True, input=stdin)

    def git(self, *args, cwd=None):
        p = self.run_("git", *args, cwd=cwd)
        self.assertEqual(p.returncode, 0, f"git {' '.join(args)}: {p.stderr}")
        return p.stdout.strip()

    def pl(self, *args, cwd=None):
        return self.run_(sys.executable, "-m", "plowline", *args, cwd=cwd)

    def configure(self, extra: str = "", gate: str | None = None) -> None:
        gate = gate if gate is not None else f"gate = '\"{PY}\" gate.py'"
        (self.app / "plowline.toml").write_text(f"[plowline]\nremote = \"\"\nreview = \"\"\n{gate}\n{extra}")
        self.git("add", "-A")
        self.git("commit", "-qm", "init")

    def wt(self, name: str) -> Path:
        return self.tmp / "app-lanes" / name

    def lane(self, name: str, files: dict, *new_args, done: bool = True):
        new = self.pl("new", name, *new_args)
        self.assertEqual(new.returncode, 0, new.stdout + new.stderr)
        self.commit(name, files)
        if done:
            d = self.pl("done", name)
            self.assertEqual(d.returncode, 0, d.stdout + d.stderr)
            return d
        return new

    def commit(self, name: str, files: dict, message: str = "") -> None:
        wt = self.wt(name)
        for rel, text in files.items():
            (wt / rel).parent.mkdir(parents=True, exist_ok=True)
            (wt / rel).write_text(text)
        self.git("add", "-A", cwd=wt)
        self.git("commit", "-qm", message or f"lane {name}", cwd=wt)

    def lane_json(self, name: str) -> dict:
        return json.loads((self.app / ".git" / "plowline" / "lanes" / f"{name}.json").read_text())

    def state(self, name: str) -> str:
        return self.lane_json(name)["state"]

    def trunk_file(self, rel: str) -> str:
        return self.git("show", f"main:{rel}")

    def in_process(self, *args):
        """Run `pl` in this process (so a test can patch it); returns (exit code, stdout + stderr)."""
        from plowline import cli
        out, cwd = io.StringIO(), os.getcwd()
        keys = {k: v for k, v in ENV.items() if k.startswith("GIT_")}
        old = {k: os.environ.get(k) for k in keys}
        os.environ.update(keys)
        os.chdir(self.app)
        try:
            with redirect_stdout(out), redirect_stderr(out):
                code = cli.main(list(args))
        finally:
            os.chdir(cwd)
            for k, v in old.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
        return code, out.getvalue()

    def wait_for(self, path: Path, seconds: float = 30) -> None:
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            if path.exists() and path.stat().st_size:
                time.sleep(0.3)
                return
            time.sleep(0.2)
        self.fail(f"{path} did not appear within {seconds}s")
