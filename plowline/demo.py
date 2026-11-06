"""Five lanes, one train, in a throwaway repo.

  a, b, c  land
  d        conflicts with c in shared.txt -> bounced, resumed, lands on the next train
  e        turns the gate red             -> bounced, the rest still land

Run:  pl demo
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ENV = {**os.environ, "PYTHONPATH": str(ROOT),
       "GIT_AUTHOR_NAME": "demo", "GIT_AUTHOR_EMAIL": "demo@example.com",
       "GIT_COMMITTER_NAME": "demo", "GIT_COMMITTER_EMAIL": "demo@example.com"}

GATE_SCRIPT = """import pathlib, sys
bad = [p.name for p in pathlib.Path('.').glob('*.txt') if 'BROKEN' in p.read_text()]
sys.exit(f'gate: broken files {bad}' if bad else 0)
"""


def sh(*args: str, cwd: Path, check: bool = True) -> subprocess.CompletedProcess:
    p = subprocess.run(args, cwd=cwd, env=ENV, capture_output=True, text=True)
    if check and p.returncode != 0:
        raise RuntimeError(f"{' '.join(args)} failed:\n{p.stdout}{p.stderr}")
    return p


def run(say=print) -> dict:
    tmp = Path(tempfile.mkdtemp(prefix="plowline-demo-"))
    origin, app = tmp / "origin.git", tmp / "app"
    sh("git", "init", "--quiet", "--bare", "-b", "main", str(origin), cwd=tmp)
    sh("git", "clone", "--quiet", str(origin), str(app), cwd=tmp)
    sh("git", "checkout", "--quiet", "-b", "main", cwd=app)
    (app / "check.py").write_text(GATE_SCRIPT)
    (app / "shared.txt").write_text("original\n")
    (app / "plowline.toml").write_text(
        f"[plowline]\ntrunk = \"main\"\ngate = '\"{sys.executable}\" check.py'\n")
    sh("git", "add", "-A", cwd=app)
    sh("git", "commit", "--quiet", "-m", "init", cwd=app)
    sh("git", "push", "--quiet", "-u", "origin", "main", cwd=app)

    def pl(*args: str, cwd: Path = app) -> subprocess.CompletedProcess:
        p = sh(sys.executable, "-m", "plowline", *args, cwd=cwd, check=False)
        say(f"$ pl {' '.join(args)}\n{(p.stdout + p.stderr).rstrip()}\n")
        if p.returncode == 2:
            raise RuntimeError(p.stderr)
        return p

    work = {"a": ("a.txt", "lane a\n"), "b": ("b.txt", "lane b\n"),
            "c": ("shared.txt", "changed by c\n"), "d": ("shared.txt", "changed by d\n"),
            "e": ("e.txt", "BROKEN\n")}
    say(f"# A throwaway repo in {tmp}. Five lanes start from main, one agent each.\n")
    for name, (path, _) in work.items():
        pl("new", name, "--paths", path)
    say("# Each agent commits in its own worktree, then sends its lane to review with `pl done`.\n")
    for name, (path, text) in work.items():
        wt = tmp / "app-lanes" / name
        (wt / path).write_text(text)
        sh("git", "add", path, cwd=wt)
        sh("git", "commit", "--quiet", "-m", f"lane {name}", cwd=wt)
        pl("done", name)

    say("# One train: merge every candidate onto main, run the gate once, push once.\n"
        "# d conflicts with c and e breaks the gate, so both bounce. a, b and c still land.\n")
    pl("train")
    pl("status")

    say("# The agent in lane d merges main (never a rebase), resolves, and queues the lane again.\n")
    # d was bounced for a conflict: merge main into the lane, resolve, land it.
    pl("resume", "d")
    wt = tmp / "app-lanes" / "d"
    sh("git", "merge", "--quiet", "origin/main", cwd=wt, check=False)
    (wt / "shared.txt").write_text("changed by c\nchanged by d\n")
    sh("git", "commit", "--quiet", "-am", "lane d: merge main, keep both", cwd=wt)
    pl("done", "d")
    pl("train")
    pl("clean")
    pl("status")

    log = sh("git", "log", "--oneline", "--first-parent", "origin/main", cwd=app).stdout
    states = {f.stem: json.loads(f.read_text())["state"]
              for f in (app / ".git" / "plowline" / "lanes").glob("*.json")}
    shared = sh("git", "show", "origin/main:shared.txt", cwd=app).stdout
    say(f"# main, first parent: one merge commit per landed lane\n{log}")
    return {"tmp": tmp, "log": log, "states": states, "shared": shared}


if __name__ == "__main__":
    run()
