"""Plowline: run many coding agents on one repo.

Each lane is one agent's work: its own git worktree on its own branch.
Finished lanes become candidates, and a merge train lands them on the trunk:
merge every candidate onto the current trunk, run one gate, push once.
A lane that conflicts or turns the gate red goes back to its worktree; the
rest still land. Nothing is ever rebased, so reviewed commits are shipped
commits.
"""
from __future__ import annotations

import argparse
import fnmatch
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import tomllib
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path

CONFIG_NAME = "plowline.toml"
DEFAULTS = {
    "trunk": "main",
    "remote": "origin",
    "gate": "",
    "review": "",
    "regenerate": "",
    "worktrees": "../{repo}-lanes",
    "branch_prefix": "lane/",
}
LANE_NAME = re.compile(r"^[a-z0-9][a-z0-9._-]{0,62}$")
DIFF_LIMIT = 200_000
LOCK_STALE_SECONDS = 2 * 60 * 60

REVIEW_PROMPT = """You are reviewing one lane of work before it joins a merge train.
Another agent wrote it in its own git worktree. Read the brief and the diff.

Answer GO or NO-GO on the first line, alone. Then list your findings.
Say NO-GO only for real defects: a bug, behaviour changed without a test,
work outside the brief, code that will not build, or a secret in the diff.
Style preferences are findings, not a NO-GO.

## Brief
{brief}

## Diff against {base}
{diff}
"""

BUILTIN_REVIEWERS = {
    "builtin:claude": ["claude", "-p"],
    "builtin:codex": ["codex", "exec", "-"],
}


class YardError(Exception):
    pass


def git(*args: str, cwd: Path, check: bool = True) -> str:
    p = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    if check and p.returncode != 0:
        detail = (p.stderr or p.stdout).strip()
        raise YardError(f"git {' '.join(args)} failed:\n{detail}")
    return p.stdout.strip()


def git_ok(*args: str, cwd: Path) -> bool:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True).returncode == 0


def shell(cmd: str, cwd: Path, env: dict | None = None) -> tuple[bool, str]:
    p = subprocess.run(cmd, cwd=cwd, shell=True, capture_output=True, text=True,
                       env={**os.environ, **(env or {})})
    return p.returncode == 0, (p.stdout + p.stderr).strip()


def tail(text: str, lines: int = 20) -> str:
    return "\n".join(text.splitlines()[-lines:])


def paths_overlap(a: str, b: str) -> bool:
    a, b = a.removeprefix("./").rstrip("/"), b.removeprefix("./").rstrip("/")
    if not a or not b:
        return True
    return (a == b or a.startswith(b + "/") or b.startswith(a + "/")
            or fnmatch.fnmatch(a, b) or fnmatch.fnmatch(b, a))


@dataclass
class Lane:
    name: str
    branch: str
    worktree: str
    base: str
    paths: list[str] = field(default_factory=list)
    brief: str = ""
    state: str = "active"  # active | candidate | bounced | landed
    head: str = ""
    note: str = ""
    queued_at: float = 0.0
    updated_at: float = 0.0


class Yard:
    def __init__(self, start: Path):
        common = git("rev-parse", "--path-format=absolute", "--git-common-dir", cwd=start)
        self.common = Path(common)
        self.root = self.common.parent
        self.state_dir = self.common / "plowline"
        self.lanes_dir = self.state_dir / "lanes"
        self.cfg = dict(DEFAULTS)
        cfg_file = self.root / CONFIG_NAME
        if cfg_file.exists():
            with cfg_file.open("rb") as f:
                try:
                    self.cfg.update(tomllib.load(f).get("plowline", {}))
                except tomllib.TOMLDecodeError as e:
                    raise YardError(f"{cfg_file}: {e}")

    # --- state -----------------------------------------------------------
    def lane_file(self, name: str) -> Path:
        return self.lanes_dir / f"{name}.json"

    def load(self, name: str) -> Lane:
        f = self.lane_file(name)
        if not f.exists():
            raise YardError(f"no lane named {name!r}")
        return Lane(**json.loads(f.read_text(encoding="utf-8")))

    def save(self, lane: Lane) -> None:
        lane.updated_at = time.time()
        self.lanes_dir.mkdir(parents=True, exist_ok=True)
        tmp = self.lane_file(lane.name).with_suffix(".tmp")
        tmp.write_text(json.dumps(asdict(lane), indent=2), encoding="utf-8")
        os.replace(tmp, self.lane_file(lane.name))

    def lanes(self) -> list[Lane]:
        if not self.lanes_dir.exists():
            return []
        return [self.load(f.stem) for f in sorted(self.lanes_dir.glob("*.json"))]

    @contextmanager
    def lock(self):
        self.state_dir.mkdir(parents=True, exist_ok=True)
        path = self.state_dir / "train.lock"
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            age = time.time() - path.stat().st_mtime
            hint = " (it looks stale; delete it if no train is running)" if age > LOCK_STALE_SECONDS else ""
            raise YardError(f"another train holds {path}{hint}")
        try:
            os.write(fd, str(os.getpid()).encode())
            os.close(fd)
            yield
        finally:
            path.unlink(missing_ok=True)

    # --- git helpers -----------------------------------------------------
    def has_remote(self) -> bool:
        remote = self.cfg["remote"]
        return bool(remote) and remote in git("remote", cwd=self.root).split()

    def trunk_ref(self, fetch: bool = False) -> str:
        trunk = self.cfg["trunk"]
        if self.has_remote():
            if fetch:
                git("fetch", "--quiet", self.cfg["remote"], trunk, cwd=self.root)
            return f"{self.cfg['remote']}/{trunk}"
        return trunk

    def worktree_dir(self, name: str) -> Path:
        base = Path(self.cfg["worktrees"].format(repo=self.root.name))
        if not base.is_absolute():
            base = self.root / base
        return (base / name).resolve()

    def overlaps(self, lane: Lane) -> list[tuple[str, str, str]]:
        found = []
        for other in self.lanes():
            if other.name == lane.name or other.state == "landed":
                continue
            for p in lane.paths:
                for q in other.paths:
                    if paths_overlap(p, q):
                        found.append((other.name, p, q))
        return found

    # --- review ----------------------------------------------------------
    def review(self, lane: Lane, head: str, base: str) -> tuple[bool, str]:
        spec = str(self.cfg["review"]).strip()
        if not spec:
            return True, "no reviewer configured"
        wt = Path(lane.worktree)
        if spec in BUILTIN_REVIEWERS:
            argv = list(BUILTIN_REVIEWERS[spec])
            exe = shutil.which(argv[0])
            if not exe:
                raise YardError(f"{spec} needs {argv[0]!r} on PATH")
            argv[0] = exe
            diff = git("diff", f"{base}...{head}", cwd=self.root)
            if len(diff) > DIFF_LIMIT:
                diff = diff[:DIFF_LIMIT] + "\n[diff truncated by plowline]"
            prompt = REVIEW_PROMPT.format(brief=lane.brief or "(no brief recorded)", base=base, diff=diff)
            p = subprocess.run(argv, input=prompt, cwd=wt, capture_output=True, text=True, encoding="utf-8")
            out = p.stdout.strip()
            if p.returncode != 0:
                return False, f"reviewer exited {p.returncode}: {tail(p.stderr or out)}"
            first = next((ln.strip().upper() for ln in out.splitlines() if ln.strip()), "")
            first = first.strip("*#` ")
            return first.startswith("GO"), out
        env = {"PLOWLINE_LANE": lane.name, "PLOWLINE_BRANCH": lane.branch,
               "PLOWLINE_BASE": base, "PLOWLINE_HEAD": head,
               "PLOWLINE_WORKTREE": str(wt)}
        return shell(spec, wt, env)


# --- commands -------------------------------------------------------------
def cmd_init(y: Yard, a) -> int:
    cfg_file = y.root / CONFIG_NAME
    if cfg_file.exists():
        print(f"{cfg_file} already exists")
        return 0
    trunk = git("symbolic-ref", "--short", "-q", "refs/remotes/origin/HEAD", cwd=y.root, check=False)
    trunk = trunk.split("/", 1)[1] if "/" in trunk else (git("branch", "--show-current", cwd=y.root) or "main")
    gate = ""
    for marker, cmd in (("pyproject.toml", "python -m pytest -q"), ("package.json", "npm test"),
                        ("go.mod", "go test ./..."), ("Cargo.toml", "cargo test"),
                        ("pom.xml", "mvn -q test"), ("Makefile", "make test")):
        if (y.root / marker).exists():
            gate = cmd
            break
    cfg_file.write_text(
        "[plowline]\n"
        f'trunk = "{trunk}"\n'
        'remote = "origin"\n'
        f'gate = "{gate}"          # must pass on the merged train before anything is pushed\n'
        'review = ""            # "builtin:claude", "builtin:codex", any command (exit 0 = GO), or "" for none\n'
        'regenerate = ""        # optional: rebuilds generated files on the merged tree before the gate\n'
        'worktrees = "../{repo}-lanes"\n',
        encoding="utf-8")
    print(f"wrote {cfg_file}")
    if not gate:
        print("set `gate` to your test command before running a train")
    print("commit plowline.toml to the trunk so every lane sees it")
    return 0


def cmd_new(y: Yard, a) -> int:
    name = a.name
    if not LANE_NAME.match(name):
        raise YardError("lane names are lowercase letters, digits, '.', '_' or '-'")
    branch = y.cfg["branch_prefix"] + name
    wt = y.worktree_dir(name)
    if y.lane_file(name).exists():
        raise YardError(f"lane {name!r} already exists; use `yard resume {name}` or pick a new name")
    if git_ok("show-ref", "--verify", "--quiet", f"refs/heads/{branch}", cwd=y.root):
        raise YardError(f"branch {branch} already exists")
    if y.has_remote() and git_ok("ls-remote", "--exit-code", "--heads", y.cfg["remote"], branch, cwd=y.root):
        raise YardError(f"branch {branch} already exists on {y.cfg['remote']}")
    if wt.exists():
        raise YardError(f"{wt} already exists")
    base_ref = y.trunk_ref(fetch=True)
    base = git("rev-parse", base_ref, cwd=y.root)
    git("worktree", "add", "--quiet", "-b", branch, str(wt), base, cwd=y.root)
    brief = Path(a.brief).read_text(encoding="utf-8") if a.brief else ""
    lane = Lane(name=name, branch=branch, worktree=str(wt), base=base, paths=a.paths or [], brief=brief)
    y.save(lane)
    print(f"lane {name}: {wt} on {branch} from {base_ref} {base[:9]}")
    for other, p, q in y.overlaps(lane):
        print(f"  overlap: {p} touches {q} in lane {other} (allowed; the train settles it)")
    return 0


def cmd_status(y: Yard, a) -> int:
    lanes = y.lanes()
    if not lanes:
        print("no lanes")
        return 0
    trunk = y.trunk_ref()
    for lane in lanes:
        ahead = "?"
        if Path(lane.worktree).exists():
            ahead = git("rev-list", "--count", f"{trunk}..{lane.branch}", cwd=y.root, check=False) or "?"
        print(f"{lane.name:<20} {lane.state:<10} {ahead:>3} ahead  {', '.join(lane.paths) or '-'}")
        for other, p, q in ([] if lane.state == "landed" else y.overlaps(lane)):
            print(f"{'':<20} overlap with {other}: {p} ~ {q}")
        if lane.note and lane.state in ("bounced", "active"):
            print(f"{'':<20} {lane.note.splitlines()[0]}")
    return 0


def cmd_show(y: Yard, a) -> int:
    lane = y.load(a.name)
    print(f"lane:     {lane.name} ({lane.state})")
    print(f"worktree: {lane.worktree}")
    print(f"branch:   {lane.branch}")
    print(f"paths:    {', '.join(lane.paths) or '-'}")
    if lane.brief:
        print("\n## Brief\n" + lane.brief.strip())
    if lane.note:
        print("\n## Latest note\n" + lane.note.strip())
    return 0


def cmd_done(y: Yard, a) -> int:
    lane = y.load(a.name)
    if lane.state not in ("active", "bounced"):
        raise YardError(f"lane {lane.name} is {lane.state}")
    wt = Path(lane.worktree)
    if git("status", "--porcelain", cwd=wt):
        raise YardError(f"{wt} has uncommitted changes; commit them first")
    base = y.trunk_ref(fetch=True)
    head = git("rev-parse", lane.branch, cwd=y.root)
    if git("rev-list", "--count", f"{base}..{head}", cwd=y.root) == "0":
        raise YardError(f"lane {lane.name} has no commits beyond {base}")
    go, reason = (True, "review skipped") if a.no_review else y.review(lane, head, base)
    lane.note = reason
    if not go:
        lane.state = "active"
        y.save(lane)
        print(f"NO-GO for {lane.name}:\n{tail(reason, 40)}")
        return 1
    lane.state, lane.head, lane.queued_at = "candidate", head, time.time()
    y.save(lane)
    print(f"GO: {lane.name} {head[:9]} is a candidate for the next train")
    return 0


def bounce(y: Yard, lane: Lane, note: str) -> None:
    lane.state, lane.note = "bounced", note
    y.save(lane)
    print(f"bounced {lane.name}: {note.splitlines()[0]}")


def cmd_train(y: Yard, a) -> int:
    if not str(y.cfg["gate"]).strip():
        raise YardError("set `gate` in plowline.toml first")
    if not y.has_remote():
        raise YardError(f"the train pushes to remote {y.cfg['remote']!r}, which this repo does not have")
    with y.lock():
        cands = sorted((l for l in y.lanes() if l.state == "candidate"), key=lambda l: l.queued_at)
        if not cands:
            print("no candidates")
            return 0
        base_ref = y.trunk_ref(fetch=True)
        tip = git("rev-parse", base_ref, cwd=y.root)
        scratch = Path(tempfile.mkdtemp(prefix="plowline-train-"))
        shutil.rmtree(scratch)
        git("worktree", "add", "--quiet", "--detach", str(scratch), tip, cwd=y.root)
        bounced = 0
        try:
            merged: list[tuple[Lane, str]] = []
            for lane in cands:
                p = subprocess.run(["git", "merge", "--no-ff", "--no-edit", "-m",
                                    f"plowline: land {lane.branch}", lane.head],
                                   cwd=scratch, capture_output=True, text=True)
                if p.returncode != 0:
                    paths = git("diff", "--name-only", "--diff-filter=U", cwd=scratch, check=False)
                    git("merge", "--abort", cwd=scratch, check=False)
                    bounce(y, lane, f"MERGE_CONFLICT with {base_ref} {tip[:9]}: "
                                    f"{', '.join(paths.split()) or 'see git output'}\n"
                                    f"Merge {base_ref} into {lane.branch}, resolve, re-test, then `yard done {lane.name}`.")
                    bounced += 1
                    continue
                merged.append((lane, git("rev-parse", "HEAD", cwd=scratch)))
            while merged:
                if str(y.cfg["regenerate"]).strip():
                    ok, out = shell(y.cfg["regenerate"], scratch)
                    if not ok:
                        raise YardError(f"regenerate failed on the merged tree:\n{tail(out)}")
                    if git("status", "--porcelain", cwd=scratch):
                        git("add", "-A", cwd=scratch)
                        git("commit", "--quiet", "-m", "plowline: regenerate on the merged tree", cwd=scratch)
                names = ", ".join(l.name for l, _ in merged)
                print(f"gate on {len(merged)} lane(s): {names}")
                ok, out = shell(y.cfg["gate"], scratch)
                if ok:
                    break
                lane, _ = merged.pop()
                git("reset", "--quiet", "--hard", merged[-1][1] if merged else tip, cwd=scratch)
                bounce(y, lane, f"GATE_FAIL on the train with {names}; {lane.name} was added last.\n"
                                f"{tail(out)}\nMerge {base_ref} into {lane.branch}, fix, re-test, then `yard done {lane.name}`.")
                bounced += 1
            if not merged:
                print("nothing landed")
                return 1
            head = git("rev-parse", "HEAD", cwd=scratch)
            if a.dry_run:
                print(f"dry run: would push {head[:9]} to {base_ref}")
                return 0
            p = subprocess.run(["git", "push", "--quiet", y.cfg["remote"], f"HEAD:refs/heads/{y.cfg['trunk']}"],
                               cwd=scratch, capture_output=True, text=True)
            if p.returncode != 0:
                raise YardError(f"push refused (did {base_ref} move?); candidates are unchanged, run the train again\n"
                                f"{p.stderr.strip()}")
            for lane, _ in merged:
                lane.state, lane.note = "landed", f"landed in {head}"
                y.save(lane)
            print(f"landed {len(merged)} lane(s) on {base_ref} at {head[:9]}")
            return 1 if bounced else 0
        finally:
            git("worktree", "remove", "--force", str(scratch), cwd=y.root, check=False)


def cmd_resume(y: Yard, a) -> int:
    lane = y.load(a.name)
    if lane.state != "bounced":
        raise YardError(f"lane {lane.name} is {lane.state}, not bounced")
    lane.state = "active"
    y.save(lane)
    trunk = y.trunk_ref(fetch=True)
    print(f"lane {lane.name} is active again in {lane.worktree}\n")
    print(lane.note.strip())
    print(f"\nnext:\n  git -C \"{lane.worktree}\" merge {trunk}   # merge, never rebase\n"
          f"  fix, test, commit\n  yard done {lane.name}")
    return 0


def cmd_clean(y: Yard, a) -> int:
    trunk = y.trunk_ref(fetch=y.has_remote())
    for lane in y.lanes():
        if lane.state != "landed":
            continue
        wt = Path(lane.worktree)
        if wt.exists():
            if git("status", "--porcelain", cwd=wt, check=False):
                print(f"kept {lane.name}: {wt} has uncommitted changes")
                continue
            git("worktree", "remove", str(wt), cwd=y.root)
        if git_ok("merge-base", "--is-ancestor", lane.branch, trunk, cwd=y.root):
            git("branch", "-D", lane.branch, cwd=y.root, check=False)
        y.lane_file(lane.name).unlink()
        print(f"cleaned {lane.name}")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="yard", description="Run many coding agents on one repo.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init", help="write plowline.toml")
    p = sub.add_parser("new", help="start a lane: worktree + branch + claim")
    p.add_argument("name")
    p.add_argument("--paths", nargs="*", help="files or globs this lane expects to touch")
    p.add_argument("--brief", help="file holding the lane's brief")
    sub.add_parser("status", help="list lanes, claims and overlaps")
    p = sub.add_parser("show", help="print a lane's brief and latest note")
    p.add_argument("name")
    p = sub.add_parser("done", help="review a lane and make it a train candidate")
    p.add_argument("name")
    p.add_argument("--no-review", action="store_true")
    p = sub.add_parser("train", help="merge candidates onto the trunk, gate once, push once")
    p.add_argument("--dry-run", action="store_true")
    p = sub.add_parser("resume", help="reopen a bounced lane")
    p.add_argument("name")
    sub.add_parser("clean", help="remove worktrees and branches of landed lanes")
    a = ap.parse_args(argv)
    try:
        y = Yard(Path.cwd())
        return {"init": cmd_init, "new": cmd_new, "status": cmd_status, "show": cmd_show,
                "done": cmd_done, "train": cmd_train, "resume": cmd_resume,
                "clean": cmd_clean}[a.cmd](y, a)
    except YardError as e:
        print(f"yard: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
