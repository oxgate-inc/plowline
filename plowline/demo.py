"""Five lanes, one train, in a throwaway repo, then one `pl check`.

  a, b, c  land
  d        conflicts with c in shared.txt -> bounced, resumed, lands on the next train
  e        turns the gate red             -> bounced, the rest still land
  f        a test that passes without testing the change -> `pl check` names the line (needs mutmut)

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
IDENTITY = {"GIT_AUTHOR_NAME": "demo", "GIT_AUTHOR_EMAIL": "demo@example.com",
            "GIT_COMMITTER_NAME": "demo", "GIT_COMMITTER_EMAIL": "demo@example.com"}


def demo_env() -> dict:
    """The environment demo commands run with, read when the demo starts (a PATH set later is seen)."""
    return {**os.environ, "PYTHONPATH": str(ROOT), **IDENTITY}


ENV = demo_env()

GATE_SCRIPT = """import pathlib, sys
bad = [p.name for p in pathlib.Path('.').glob('*.txt') if 'BROKEN' in p.read_text()]
sys.exit(f'gate: broken files {bad}' if bad else 0)
"""

# Lane f: the function counts unfinished items (the condition is reversed), and its test passes anyway, because
# its one case has as many finished items as unfinished ones. Only a mutant (dropping the `not`) shows it.
COUNTER_BASE = 'def count_finished(items):\n    """How many items are finished."""\n    return 0\n'
COUNTER_LANE = ('def count_finished(items):\n    """How many items are finished."""\n'
                '    return sum(1 for item in items if not item["done"])\n')
COUNTER_TEST = ('from tasks.counter import count_finished\n\n\ndef test_counts_finished_items():\n'
                '    items = [{"done": True}, {"done": False}]\n    assert count_finished(items) == 1\n')
MUTMUT_CONFIG = '[tool.mutmut]\nsource_paths = ["tasks"]\n'


def sh(*args: str, cwd: Path, check: bool = True, env: dict | None = None) -> subprocess.CompletedProcess:
    p = subprocess.run(args, cwd=cwd, env=env or ENV, capture_output=True, text=True)
    if check and p.returncode != 0:
        raise RuntimeError(f"{' '.join(args)} failed:\n{p.stdout}{p.stderr}")
    return p


def run(say=print) -> dict:
    env = demo_env()
    tmp = Path(tempfile.mkdtemp(prefix="plowline-demo-"))
    origin, app = tmp / "origin.git", tmp / "app"
    sh("git", "init", "--quiet", "--bare", "-b", "main", str(origin), cwd=tmp)
    sh("git", "clone", "--quiet", str(origin), str(app), cwd=tmp)
    sh("git", "checkout", "--quiet", "-b", "main", cwd=app)
    (app / "check.py").write_text(GATE_SCRIPT)
    (app / "shared.txt").write_text("original\n")
    (app / "tasks").mkdir()
    (app / "tasks" / "__init__.py").write_text("")
    (app / "tasks" / "counter.py").write_text(COUNTER_BASE)
    (app / "pyproject.toml").write_text(MUTMUT_CONFIG)
    (app / "plowline.toml").write_text(
        f"[plowline]\ntrunk = \"main\"\ngate = '\"{sys.executable}\" check.py'\n")
    sh("git", "add", "-A", cwd=app)
    sh("git", "commit", "--quiet", "-m", "init", cwd=app)
    sh("git", "push", "--quiet", "-u", "origin", "main", cwd=app)

    def pl(*args: str, cwd: Path = app, unavailable_ok: bool = False) -> subprocess.CompletedProcess:
        p = sh(sys.executable, "-m", "plowline", *args, cwd=cwd, check=False, env=env)
        say(f"$ pl {' '.join(args)}\n{(p.stdout + p.stderr).rstrip()}\n")
        if p.returncode == 2 and not unavailable_ok:
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
        "# d conflicts with c, so it bounces before the gate. e turns the gate red: the train\n"
        "# splits until it finds e, bounces it with the gate output, and a, b and c still land.\n")
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

    say("# Lane f fixes count_finished, and its test passes. But the test uses one finished item and one\n"
        "# unfinished one, so it passes just as well for the bug the lane shipped: the condition is reversed.\n"
        "# `pl check` mutates the lines the lane changed and runs the tests; a mutant no test notices is a\n"
        "# survivor. With builtin:mutants in the gate, the train runs the same check.\n")
    pl("new", "f", "--paths", "tasks", "tests")
    wt = tmp / "app-lanes" / "f"
    (wt / "tasks" / "counter.py").write_text(COUNTER_LANE)
    (wt / "tests").mkdir(exist_ok=True)
    (wt / "tests" / "test_counter.py").write_text(COUNTER_TEST)
    sh("git", "add", "-A", cwd=wt)
    sh("git", "commit", "--quiet", "-m", "lane f: count finished items", cwd=wt)
    pl("check", "--explain", cwd=wt)
    check = pl("check", cwd=wt, unavailable_ok=True)
    if check.returncode == 2:
        say("# No mutation engine ran here. Install mutmut 3 and pytest (pip install mutmut pytest; on Windows,\n"
            f"# inside WSL) and run `pl check` in {wt} (`pl demo --keep` keeps the repo).\n"
            "# It names tasks/counter.py:3: removing the `not` makes no test fail.\n")
    return {"tmp": tmp, "log": log, "states": states, "shared": shared,
            "check": check.stdout + check.stderr, "check_code": check.returncode}


if __name__ == "__main__":
    run()
