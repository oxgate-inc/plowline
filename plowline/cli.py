"""Plowline: run many coding agents on one repo.

Each lane is one agent's work: its own git worktree on its own branch.
Finished lanes become candidates, and a merge train lands them on the trunk:
merge every candidate onto the current trunk, run one gate, push once.
A lane that conflicts or turns the gate red goes back to its worktree with a
notice; the rest still land. Nothing is ever rebased, so reviewed commits are
shipped commits.
"""
from __future__ import annotations

import argparse
import dataclasses
import fnmatch
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import tomllib
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path

from plowline import gate as gates
from plowline import rules

CONFIG_NAME = "plowline.toml"
DEFAULTS = {
    "trunk": "main",
    "remote": "origin",
    "gate": "",
    "review": "",
    "regenerate": "",
    "generated": [],
    "worktrees": "../{repo}-lanes",
    "branch_prefix": "lane/",
    "agent": "claude",
    "repair": "",
    "repair_attempts": 2,
    "land": "push",
    "on_land": "",
    "env": "",
    "gh": "gh",
    "strict_paths": False,
    "max_lanes": 0,
}
LANE_NAME = re.compile(r"^[a-z0-9][a-z0-9._-]{0,62}$")
DIFF_LIMIT = 200_000
LOCK_STALE_SECONDS = 2 * 60 * 60
STANCHION_TIMEOUT = 300  # seconds; `stanchion up` copies a database and may run a setup hook
NOTES_DIR = ".plowline"  # notices, follow-ups, logs and backups people and agents read; gitignored by setup
MERGE_TREE_GIT = (2, 38)  # `git merge-tree --write-tree --name-only`

EVIDENCE_BLOCK = """COMMIT: <the commit you finished on>
COMMANDS RUN: <each command you ran, with its exit code>
RESULT: <what passed or failed, with counts>
NOT VERIFIED: <what you could not check, or "nothing">"""

LANE_PROMPT = ("You are working in the Plowline lane {name}. Run pl show {name} to read your brief, "
               "and follow the lane rules in AGENTS.md or CLAUDE.md. When your work is committed and the tests "
               "pass, run pl done {name}. If it answers NO-GO, fix the findings and run it again. End your final "
               "report with four lines: COMMIT, COMMANDS RUN, RESULT and NOT VERIFIED.")

REVIEW_PROMPT = """You are reviewing one lane of work before it joins a merge train.
Another agent wrote it in its own git worktree. Read the brief and the diff.

Answer GO or NO-GO on the first line, alone. Then list blocking findings under
"## Blocking", and anything worth doing later that should not hold this lane
back under "## Follow-ups", one "- " line each.
Say NO-GO only for real defects: a bug, behaviour changed without a test,
work outside the brief, code that will not build, or a secret in the diff.
Style preferences are follow-ups, not a NO-GO.

End with this block, filled in:
""" + EVIDENCE_BLOCK.replace("you finished on", "you reviewed") + """

## Brief
{brief}

## Diff against {base} (head {head})
{diff}
"""

AGENT_CLIS = ("claude", "codex", "gemini", "aider", "cursor-agent", "opencode")  # setup picks the first on PATH

BUILTIN_REVIEWERS = {
    "builtin:claude": ["claude", "-p"],
    "builtin:codex": ["codex", "exec", "-"],
}


class YardError(Exception):
    pass


class LockBusy(YardError):
    """Another train (or full-steam loop) holds the lock: wait, it is not a failure."""



def git(*args: str, cwd: Path, check: bool = True) -> str:
    p = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if check and p.returncode != 0:
        detail = (p.stderr or p.stdout).strip()
        raise YardError(f"git {' '.join(args)} failed:\n{detail}")
    return p.stdout.strip()


def git_run(*args: str, cwd: Path) -> subprocess.CompletedProcess:
    """git with its own exit code kept, for the callers that decide on it."""
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace")


def git_ok(*args: str, cwd: Path) -> bool:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True).returncode == 0


def shell(cmd: str, cwd: Path, env: dict | None = None) -> tuple[bool, str]:
    p = subprocess.run(cmd, cwd=cwd, shell=True, capture_output=True, text=True, encoding="utf-8", errors="replace",
                       env={**os.environ, **(env or {})}, **gates.child_group())
    return p.returncode == 0, (p.stdout + p.stderr).strip()


def command_argv(cmd: str) -> list[str]:
    """Split a configured command. On Windows, backslashes in paths are kept and quotes only group."""
    if os.name != "nt":
        return shlex.split(cmd)
    return [a[1:-1] if len(a) > 1 and a[0] == a[-1] == '"' else a for a in shlex.split(cmd, posix=False)]


def quote_arg(value: str) -> str:
    return f'"{value}"' if os.name == "nt" else shlex.quote(value)


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
    state: str = "active"  # active | candidate | proposed | bounced | landed
    head: str = ""
    note: str = ""
    queued_at: float = 0.0
    updated_at: float = 0.0
    brief_path: str = ""  # the brief file, read live by `pl show` and the reviewer
    after: str = ""  # a lane that must land before this one may join a train
    priority: int = 0  # higher joins the train first
    reviewed_head: str = ""  # the commit a GO verdict is bound to
    reviewed_trunk: str = ""  # the trunk commit when that GO was given
    repairs: int = 0  # automatic repair launches so far


LANE_FIELDS = {f.name for f in dataclasses.fields(Lane)}


class Yard:
    def __init__(self, start: Path):
        common = git("rev-parse", "--path-format=absolute", "--git-common-dir", cwd=start)
        self.common = Path(common)
        self.root = self.common.parent
        self.state_dir = self.common / "plowline"
        self.lanes_dir = self.state_dir / "lanes"
        self.pr_file = self.state_dir / "train-pr.json"
        self.notes = self.root / NOTES_DIR
        self.cfg = dict(DEFAULTS)
        self.stanchion: dict | None = None  # the [stanchion] table: lane environments through Stanchion
        self._git_version: tuple[int, int] | None = None
        self.events: list[tuple[str, str, str]] = []  # (landed|bounced, branch, reason) for full steam's line
        cfg_file = self.root / CONFIG_NAME
        if cfg_file.exists():
            with cfg_file.open("rb") as f:
                try:
                    data = tomllib.load(f)
                except tomllib.TOMLDecodeError as e:
                    raise YardError(f"{cfg_file}: {e}")
            self.cfg.update(data.get("plowline", {}))
            self.stanchion = data.get("stanchion")

    # --- state -----------------------------------------------------------
    def lane_file(self, name: str) -> Path:
        return self.lanes_dir / f"{name}.json"

    def load(self, name: str) -> Lane:
        f = self.lane_file(name)
        if not f.exists():
            raise YardError(f"no lane named {name!r}")
        data = json.loads(f.read_text(encoding="utf-8"))
        return Lane(**{k: v for k, v in data.items() if k in LANE_FIELDS})

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

    def notice_file(self, name: str) -> Path:
        return self.notes / "notices" / f"{name}.md"

    def log_file(self, name: str) -> Path:
        return self.notes / "logs" / f"{name}.log"

    def followups_file(self) -> Path:
        return self.notes / "followups.md"

    def log(self, name: str, text: str) -> None:
        f = self.log_file(name)
        f.parent.mkdir(parents=True, exist_ok=True)
        with f.open("a", encoding="utf-8") as out:
            out.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S')} {text}\n")

    def parked(self, lane: Lane) -> bool:
        """A lane whose worktree is gone. It keeps its branch and state; it just can't be worked in."""
        return lane.state != "landed" and not Path(lane.worktree).exists()

    def shown_state(self, lane: Lane) -> str:
        return "parked" if self.parked(lane) else lane.state

    def need_worktree(self, lane: Lane) -> Path:
        wt = Path(lane.worktree)
        if not wt.exists():
            raise YardError(f"lane {lane.name} is parked: {wt} is missing. Recreate it with "
                            f"`git worktree prune` and `git worktree add \"{wt}\" {lane.branch}`")
        return wt

    def waiting_on(self, lane: Lane) -> str:
        """The --after lane this one still waits for, or "" once it has landed (or was cleaned after landing)."""
        if lane.after and self.lane_file(lane.after).exists() and self.load(lane.after).state != "landed":
            return lane.after
        return ""

    def brief_text(self, lane: Lane) -> str:
        if lane.brief_path and Path(lane.brief_path).exists():
            return Path(lane.brief_path).read_text(encoding="utf-8")
        return lane.brief

    @contextmanager
    def lock(self, name: str = "train.lock", what: str = "train", stale_after: float | None = None):
        """An exclusive lock file in .plowline/. With stale_after, an older lock is reported and removed."""
        self.notes.mkdir(parents=True, exist_ok=True)
        path = self.notes / name
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            try:
                age = time.time() - path.stat().st_mtime
            except FileNotFoundError:
                age = 0.0
            if stale_after is None or age <= stale_after:
                hint = (" (it looks stale; delete it if no train is running)"
                        if stale_after is None and age > LOCK_STALE_SECONDS else "")
                raise LockBusy(f"another {what} holds {path}{hint}")
            print(f"removed a stale {what} lock {path} ({int(age)}s old, past the {int(stale_after)}s cycle timeout)")
            path.unlink(missing_ok=True)
            try:
                fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                raise LockBusy(f"another {what} holds {path}")
        try:
            os.write(fd, str(os.getpid()).encode())
            os.close(fd)
            yield path
        finally:
            path.unlink(missing_ok=True)

    # --- config ----------------------------------------------------------
    def gate_steps(self) -> list[str]:
        try:
            return gates.gate_steps(self.cfg["gate"])
        except gates.GateConfigError as e:
            raise YardError(f"plowline.toml: {e}")

    def generated(self) -> list[str]:
        value = self.cfg["generated"]
        return [value] if isinstance(value, str) and value else [v for v in value if isinstance(v, str)]

    def agent_command(self, lane: Lane, key_name: str = "agent") -> str:
        """`agent` (what pl open starts) or `repair` (what fixes a bounced lane), with {lane}, {path} and
        {notice} filled in. Paths are filled in already quoted for the platform (double quotes on Windows,
        shlex.quote elsewhere), so a template writes {path}, never "{path}"."""
        cmd = str(self.cfg[key_name])
        for key, value in (("{lane}", lane.name), ("{path}", lane.worktree),
                           ("{notice}", str(self.notice_file(lane.name)))):
            cmd = cmd.replace(key, quote_arg(value) if key != "{lane}" else value)
        return cmd

    # --- git helpers -----------------------------------------------------
    def git_version(self) -> tuple[int, int]:
        if self._git_version is None:
            self._git_version = rules.git_version(git("--version", cwd=self.root))
        return self._git_version

    def merge_tree_ok(self) -> bool:
        return self.git_version() >= MERGE_TREE_GIT

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

    def trunk_checkout(self) -> Path | None:
        """The worktree that has the trunk branch checked out, if any."""
        for block in git("worktree", "list", "--porcelain", cwd=self.root).split("\n\n"):
            lines = block.splitlines()
            if f"branch refs/heads/{self.cfg['trunk']}" in lines:
                return Path(lines[0].removeprefix("worktree "))
        return None

    def land_local(self, head: str, tip: str) -> None:
        """No remote: move the local trunk to the train's head, updating its checkout if it has one."""
        trunk = self.cfg["trunk"]
        path = self.trunk_checkout()
        if path is not None:
            if git("status", "--porcelain", "--untracked-files=no", cwd=path):
                raise YardError(f"{path} has uncommitted changes on {trunk}; commit or stash them, "
                                "then run the train again (candidates are unchanged)")
            git("merge", "--ff-only", "--quiet", head, cwd=path)
            return
        git("update-ref", f"refs/heads/{trunk}", head, tip, cwd=self.root)

    def gh(self, *args: str, stdin: str | None = None) -> str:
        argv = shlex.split(str(self.cfg["gh"]))
        exe = shutil.which(argv[0]) if argv else None
        if not exe:
            raise YardError(f"land = \"pr\" needs the GitHub CLI ({self.cfg['gh']!r}) on PATH")
        p = subprocess.run([exe, *argv[1:], *args], cwd=self.root, input=stdin,
                           capture_output=True, text=True, encoding="utf-8")
        if p.returncode != 0:
            raise YardError(f"{self.cfg['gh']} {' '.join(args[:2])} failed:\n{(p.stderr or p.stdout).strip()}")
        return p.stdout.strip()

    def after_land(self, lanes: list[Lane], commit: str, pr: str = "") -> bool:
        """Run the on_land hook once per landing. A failure is reported; the lanes stay landed."""
        cmd = str(self.cfg["on_land"]).strip()
        if not cmd:
            return True
        env = {"PLOWLINE_TRUNK": self.cfg["trunk"], "PLOWLINE_COMMIT": commit,
               "PLOWLINE_LANES": " ".join(l.name for l in lanes),
               "PLOWLINE_BRANCHES": " ".join(l.branch for l in lanes), "PLOWLINE_PR": pr}
        ok, out = shell(cmd, self.root, env)
        print(f"on_land: {'ok' if ok else 'FAILED (the lanes stay landed)'}" + (f"\n{tail(out)}" if out else ""))
        return ok

    def lane_env(self, verb: str, lane: Lane) -> None:
        """Optional Stanchion hook: `stanchion up` gives a lane its own ports and database,
        `stanchion down` frees them. Never stops Plowline: a missing or failing Stanchion is
        reported and the lane carries on."""
        env = str(self.cfg["env"])
        if env not in ("", "stanchion", "off"):
            print(f"plowline.toml: env = {env!r} is not known (use \"stanchion\" or \"off\"), so it is ignored")
        if env == "off" or (env != "stanchion" and self.stanchion is None):
            return
        table = self.stanchion or {}
        argv = command_argv(str(table.get("command", "stanchion")))
        exe = shutil.which(argv[0]) if argv else None
        if not exe:
            print(f"stanchion: {argv[0] if argv else 'stanchion'} is not on PATH, so lane {lane.name} shares "
                  "your ports and database (pipx install git+https://github.com/oxgate-inc/stanchion)")
            return
        args = [exe, *argv[1:], verb, lane.name] + (["--path", lane.worktree] if verb == "up" else [])
        timeout = table.get("timeout", STANCHION_TIMEOUT)
        # Output goes to a file, not a pipe: after a timeout a grandchild (a launcher's Python) could
        # hold a pipe open and keep us waiting.
        with tempfile.TemporaryFile() as f:
            try:
                p = subprocess.run(args, cwd=self.root, stdout=f, stderr=subprocess.STDOUT, timeout=timeout)
                failed = f"exit code {p.returncode}" if p.returncode != 0 else ""
            except subprocess.TimeoutExpired:
                failed = f"timed out after {timeout} seconds"
            except OSError as e:
                failed = str(e)
            f.seek(0)
            out = f.read().decode("utf-8", "replace").strip()
        if failed:
            print(f"stanchion {verb} {lane.name} failed (the lane carries on): {failed}"
                  + (f"\n{tail(out)}" if out else ""))
        elif out:
            print(out)

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
        """GO or NO-GO for exactly `head`. NO-GO in the reviewer's first lines always wins."""
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
            prompt = REVIEW_PROMPT.format(brief=self.brief_text(lane) or "(no brief recorded)", base=base,
                                          head=head, diff=diff)
            p = subprocess.run(argv, input=prompt, cwd=wt, capture_output=True, text=True, encoding="utf-8")
            out = p.stdout.strip()
            if p.returncode != 0:
                return False, f"reviewer exited {p.returncode}: {tail(p.stderr or out)}"
            token = rules.verdict_token(out)
            if not token:
                return False, f"the reviewer gave no GO or NO-GO in its first lines\n{out}"
            return token == "GO", out
        env = {"PLOWLINE_LANE": lane.name, "PLOWLINE_BRANCH": lane.branch,
               "PLOWLINE_BASE": base, "PLOWLINE_HEAD": head,
               "PLOWLINE_WORKTREE": str(wt)}
        ok, out = shell(spec, wt, env)
        if ok and rules.verdict_token(out) == "NO-GO":
            return False, out
        return ok, out


# --- notices, repair, follow-ups --------------------------------------------
def write_notice(y: Yard, lane: Lane, reason: str, detail: str, paths: list[str], path_label: str,
                 output: str, fix: str) -> Path:
    trunk = y.trunk_ref()
    parts = [f"# Lane {lane.name} bounced: {reason}\n", detail.strip() + "\n",
             f"- Lane head: {lane.head[:12] or '-'} on {lane.branch}\n- Worktree: {lane.worktree}\n"
             f"- Trunk: {trunk}\n- When: {time.strftime('%Y-%m-%d %H:%M:%S')}\n"]
    if paths:
        parts.append(f"## {path_label}\n" + "".join(f"- {p}\n" for p in paths))
    if output.strip():
        parts.append(f"## Gate output (last 40 lines)\n```text\n{tail(output, 40)}\n```\n")
    parts.append("## What to do\n"
                 f"1. Work in {lane.worktree}, on {lane.branch}.\n"
                 f"2. Merge the trunk into your branch: `git merge {trunk}`. Never rebase: the commits that were "
                 "reviewed are the commits that ship.\n"
                 f"3. {fix}\n"
                 "4. Run the tests and commit. Leave the worktree clean.\n"
                 f"5. Run `pl done {lane.name}` to queue the lane again.\n"
                 "6. End your report with this block, filled in:\n\n"
                 + "".join(f"    {line}\n" for line in EVIDENCE_BLOCK.splitlines()))
    f = y.notice_file(lane.name)
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text("\n".join(parts), encoding="utf-8")
    return f


def bounce(y: Yard, lane: Lane, reason: str, detail: str, *, paths: list[str] | None = None,
           path_label: str = "Conflicting paths", output: str = "", fix: str = "Fix the cause above.",
           repair: bool = True, env_down: bool = True) -> None:
    paths = paths or []
    lane.state = "bounced"
    lane.note = (f"{reason}: {detail.strip()}" + (f"\n{path_label}: {', '.join(paths)}" if paths else "")
                 + (f"\n{tail(output)}" if output.strip() else ""))
    notice = write_notice(y, lane, reason, detail, paths, path_label, output, fix)
    y.save(lane)
    y.events.append(("bounced", lane.branch, reason))
    y.log(lane.name, f"bounced: {lane.note.splitlines()[0]}")
    print(f"bounced {lane.name}: {lane.note.splitlines()[0]}")
    print(f"  notice: {notice}")
    if env_down:
        y.lane_env("down", lane)
    if repair:
        auto_repair(y, lane)


def auto_repair(y: Yard, lane: Lane) -> None:
    """With `repair` set, a bounce starts that command in the lane with the notice on stdin, at most
    repair_attempts times. Then a person decides."""
    if not str(y.cfg["repair"]).strip():
        return
    cap = int(y.cfg["repair_attempts"])
    if cap <= 0:
        return
    if lane.repairs >= cap:
        msg = (f"lane {lane.name} bounced again after {lane.repairs} automatic repair attempt(s): "
               "split this lane or re-plan")
        print(msg)
        y.log(lane.name, msg)
        with y.notice_file(lane.name).open("a", encoding="utf-8") as f:
            f.write(f"\n## Stopped\n{msg}. Plowline will not start the agent again for this lane.\n")
        return
    lane.repairs += 1
    lane.state = "active"
    y.save(lane)
    y.lane_env("up", lane)
    cmd = y.agent_command(lane, "repair")
    argv = command_argv(cmd)
    exe = shutil.which(argv[0]) if argv else None
    if not exe:
        lane.state = "bounced"
        y.save(lane)
        print(f"auto-repair: {argv[0] if argv else '(empty repair)'} is not on PATH; lane {lane.name} stays bounced")
        return
    y.log(lane.name, f"auto-repair {lane.repairs}/{cap}: {cmd}")
    env = {**os.environ, "PLOWLINE_LANE": lane.name, "PLOWLINE_NOTICE": str(y.notice_file(lane.name)),
           "PLOWLINE_WORKTREE": lane.worktree, "PLOWLINE_REPAIR": str(lane.repairs)}
    flags = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW} \
        if os.name == "nt" else {"start_new_session": True}
    # The notice goes in on stdin, never in argv: it is long and holds gate output.
    with y.notice_file(lane.name).open("rb") as stdin, y.log_file(lane.name).open("ab") as log:
        subprocess.Popen([exe, *argv[1:]], cwd=lane.worktree, stdin=stdin, stdout=log, stderr=subprocess.STDOUT,
                         env=env, **flags)
    print(f"auto-repair {lane.repairs}/{cap}: started the agent in lane {lane.name} with its notice")


def record_followups(y: Yard, lane: Lane, head: str, review: str) -> int:
    items = rules.extract_followups(review)
    if not items:
        return 0
    f = y.followups_file()
    text = f.read_text(encoding="utf-8") if f.exists() else (
        "# Follow-ups\n\nNon-blocking review findings, added by `pl done`. Delete a line once it is done "
        "or dropped.\n")
    new = [i for i in items if f"- {i}\n" not in text]
    if not new:
        return 0
    text = text.rstrip("\n") + f"\n\n## {lane.name} at {head[:9]} ({time.strftime('%Y-%m-%d')})\n" + "".join(
        f"- {i}\n" for i in new)
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(text, encoding="utf-8")
    return len(new)


def followup_count(y: Yard) -> int:
    f = y.followups_file()
    if not f.exists():
        return 0
    return sum(1 for line in f.read_text(encoding="utf-8").splitlines() if line.startswith("- "))


# --- commands -------------------------------------------------------------
AGENT_RULES_HEADING = "## Working in a Plowline lane"
GATE_GUESSES = (("pyproject.toml", "python -m pytest -q"), ("package.json", "npm test"),
                ("go.mod", "go test ./..."), ("Cargo.toml", "cargo test"),
                ("pom.xml", "mvn -q test"), ("Makefile", "make test"))


def template(name: str) -> str:
    return (Path(__file__).parent / "templates" / name).read_text(encoding="utf-8")


def ask(prompt: str, default: str, interactive: bool) -> str:
    if not interactive:
        return default
    answer = input(f"{prompt} [{default}]: ").strip()
    return "" if answer == "-" else answer or default


def confirm(prompt: str, interactive: bool, default: bool) -> bool:
    if not interactive:
        return default
    answer = input(f"{prompt} [{'Y/n' if default else 'y/N'}]: ").strip().lower()
    return default if not answer else answer.startswith("y")


def cmd_setup(y: Yard, a) -> int:
    """Interactive wizard for people; with --yes (or no terminal) it runs unattended for agents."""
    interactive = not a.yes and sys.stdin.isatty()
    say = print
    version = y.git_version()
    if version < (2, 31):
        raise YardError(f"git 2.31 or newer is needed (found {'.'.join(map(str, version))})")
    remotes = git("remote", cwd=y.root).split()

    cfg_file = y.root / CONFIG_NAME
    if cfg_file.exists() and not a.force:
        say(f"keeping the existing {CONFIG_NAME} (use --force to rewrite it)")
    else:
        remote = a.remote if a.remote is not None else ("origin" if "origin" in remotes else (remotes or [""])[0])
        head = git("symbolic-ref", "--short", "-q", f"refs/remotes/{remote}/HEAD", cwd=y.root, check=False)
        trunk = a.trunk or (head.split("/", 1)[1] if "/" in head else git("branch", "--show-current", cwd=y.root) or "main")
        gate = a.gate if a.gate is not None else next(
            (cmd for marker, cmd in GATE_GUESSES if (y.root / marker).exists()), "")
        review = a.review if a.review is not None else next(
            (spec for spec, argv in BUILTIN_REVIEWERS.items() if shutil.which(argv[0])), "")
        if interactive:
            say("Plowline setup. Press Enter to keep a default, '-' to leave a value empty.\n")
        remote = ask("Remote the train pushes to ('-' for local-only: the train updates your local trunk)",
                     remote, interactive)
        trunk = ask("Trunk branch", trunk, interactive)
        gate = ask("Gate: the command that must pass before a push", gate, interactive)
        review = ask("Reviewer (builtin:claude, builtin:codex, a command, or '-' for none)", review, interactive)
        agent = ask("Command that starts an agent in a lane (used by pl open)", a.agent or next((c for c in AGENT_CLIS if shutil.which(c)), "claude"), interactive)
        regenerate = ask("Command that rebuilds generated files ('-' for none)", a.regenerate or "", interactive) \
            if interactive or a.regenerate else ""
        land, on_land = a.land or "push", a.on_land or ""
        if not gate:
            raise YardError("a gate command is required (pass --gate, e.g. --gate \"npm test\")")
        if land == "pr" and not remote:
            raise YardError("--land pr needs a remote")
        cfg_file.write_text(
            "[plowline]\n"
            f"trunk = {json.dumps(trunk)}\n"
            f"remote = {json.dumps(remote)}\n"
            f"gate = {json.dumps(gate)}  # must pass on the merged train; a list of commands works too\n"
            f"review = {json.dumps(review)}  # builtin:claude, builtin:codex, any command (exit 0 = GO), or \"\"\n"
            f"regenerate = {json.dumps(regenerate)}  # optional: rebuilds generated files on the merged tree\n"
            "generated = []  # files regenerate rebuilds, e.g. [\"package-lock.json\"]: a conflict only there takes the trunk's side\n"
            f"agent = {json.dumps(agent)}  # what `pl open` starts in a lane's terminal\n"
            "repair = \"\"  # optional: a command that fixes a bounced lane by itself (gets the notice on stdin)\n"
            f"land = {json.dumps(land)}  # \"push\" to the trunk, or \"pr\": open one pull request per train\n"
            f"on_land = {json.dumps(on_land)}  # optional: runs after each landing (deploy, tag, notify)\n"
            "strict_paths = false  # true: a lane that changes files outside its --paths bounces at pl done\n"
            "max_lanes = 0  # cap on lanes not yet landed; 0 for no cap\n"
            'worktrees = "../{repo}-lanes"\n',
            encoding="utf-8")
        y.cfg.update(trunk=trunk, remote=remote, gate=gate, review=review, regenerate=regenerate, agent=agent,
                     land=land, on_land=on_land)
        say(f"wrote {CONFIG_NAME}")

    if not a.skip_gate_check:
        steps = y.gate_steps()
        say(f"checking the gate on this checkout: {', '.join(steps)}")
        head = git("rev-parse", "HEAD", cwd=y.root, check=False)
        result = gates.run_gate(steps, gates.GateContext(tree=y.root, base=head, head=head))
        if not result.ok:
            raise YardError(f"the gate fails before any lane exists; fix the trunk first\n{result.line}\n"
                            f"{tail(result.output)}")
        say("gate passes")

    touched = [CONFIG_NAME]
    ignore = y.root / ".gitignore"
    ignored = ignore.read_text(encoding="utf-8") if ignore.exists() else ""
    if f"{NOTES_DIR}/" not in ignored.splitlines():
        ignore.write_text((ignored.rstrip("\n") + "\n" if ignored.strip() else "") + f"{NOTES_DIR}/\n",
                          encoding="utf-8")
        say(f"added {NOTES_DIR}/ to .gitignore (notices, follow-ups and logs stay local)")
        touched.append(".gitignore")
    if not a.no_agent_files:
        rules_text = template("AGENTS-snippet.md")
        for name in ("AGENTS.md", "CLAUDE.md"):
            f = y.root / name
            text = f.read_text(encoding="utf-8") if f.exists() else ""
            if AGENT_RULES_HEADING not in text:
                f.write_text((text.rstrip() + "\n\n" if text.strip() else "") + rules_text, encoding="utf-8")
                say(f"added lane rules to {name}")
                touched.append(name)
        for rel, name in (("briefs/TEMPLATE.md", "lane-brief.md"),
                          (".claude/skills/plowline/SKILL.md", "dispatcher-skill.md")):
            f = y.root / rel
            if not f.exists():
                f.parent.mkdir(parents=True, exist_ok=True)
                f.write_text(template(name), encoding="utf-8")
                say(f"wrote {rel}")
                touched.append(rel)

    if confirm("Commit these files on the current branch?", interactive, a.commit):
        git("add", "--", *touched, cwd=y.root)
        if git("diff", "--cached", "--name-only", cwd=y.root):
            git("commit", "--quiet", "-m", "Add Plowline lane setup", cwd=y.root)
            say("committed: Add Plowline lane setup")
    if not y.has_remote():
        say(f"local-only mode: lanes branch from your local {y.cfg['trunk']}, and the train updates it")
    elif confirm(f"Push to {y.cfg['remote']}/{y.cfg['trunk']} now?", interactive, a.push):
        git("push", "--quiet", y.cfg["remote"], f"HEAD:refs/heads/{y.cfg['trunk']}", cwd=y.root)
        say(f"pushed to {y.cfg['remote']}/{y.cfg['trunk']}")
    else:
        say(f"lanes branch from {y.cfg['remote']}/{y.cfg['trunk']}: push the setup before `pl new`")
    say("\nnext:\n  pl new <lane> --paths <dirs or files> --brief --open\n"
        "  pl watch     # in this terminal: status, and a train whenever a lane is done")
    return 0


def scaffold_brief(y: Yard, name: str, paths: list[str]) -> Path:
    f = y.root / "briefs" / f"{name}.md"
    if f.exists():
        print(f"brief: using the existing {f}")
        return f
    text = template("lane-brief.md").replace("<name>", name)
    if paths:
        text = text.replace("- <files or directories>\n", "".join(f"- {p}\n" for p in paths))
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(text, encoding="utf-8")
    print(f"brief: wrote {f}; fill in the goal and acceptance before the agent starts")
    return f


def retire_old_lane_files(y: Yard, name: str) -> None:
    """A name used by an earlier, cleaned lane: move its log, notice and backups to <name>.<n> so the new lane
    starts fresh and nothing of the old one is lost or mixed in."""
    old = [p for p in (y.log_file(name), y.notice_file(name), y.notes / "backups" / name) if p.exists()]
    if not old:
        return
    n = 1
    while any((p.with_name(f"{name}.{n}{p.suffix}")).exists() for p in old):
        n += 1
    for p in old:
        p.rename(p.with_name(f"{name}.{n}{p.suffix}"))
    print(f"lane name {name} was used before; its old log and notice are kept as {name}.{n}")


def cmd_new(y: Yard, a) -> int:
    name = a.name
    if not LANE_NAME.match(name):
        raise YardError("lane names are lowercase letters, digits, '.', '_' or '-'")
    branch = y.cfg["branch_prefix"] + name
    wt = y.worktree_dir(name)
    if y.lane_file(name).exists():
        raise YardError(f"lane {name!r} already exists; use `pl resume {name}` or pick a new name")
    if git_ok("show-ref", "--verify", "--quiet", f"refs/heads/{branch}", cwd=y.root):
        raise YardError(f"branch {branch} already exists")
    if y.has_remote() and git_ok("ls-remote", "--exit-code", "--heads", y.cfg["remote"], branch, cwd=y.root):
        raise YardError(f"branch {branch} already exists on {y.cfg['remote']}")
    if wt.exists():
        raise YardError(f"worktree {wt} already exists")
    cap = int(y.cfg["max_lanes"] or 0)
    open_lanes = [l.name for l in y.lanes() if l.state != "landed"]
    if cap > 0 and len(open_lanes) >= cap:
        raise YardError(f"max_lanes = {cap} reached ({', '.join(open_lanes)}); land or clean a lane first")
    if a.after and not y.lane_file(a.after).exists():
        raise YardError(f"--after {a.after}: no lane named {a.after!r}")
    checkout = y.trunk_checkout()
    remote = y.has_remote()
    dirty = git("status", "--porcelain", "--untracked-files=no", cwd=checkout).splitlines() if checkout else []
    if dirty and not remote:
        raise YardError(f"{checkout} has uncommitted changes ({len(dirty)} file(s)) on {y.cfg['trunk']}; commit "
                        "them first, so the lane starts from them")
    retire_old_lane_files(y, name)
    paths = [p.replace("\\", "/") for p in (a.paths or [])]
    brief_path = ""
    if a.brief is True:
        brief_path = str(scaffold_brief(y, name, paths))
    elif a.brief:
        brief_path = str(Path(a.brief).resolve())
    brief = Path(brief_path).read_text(encoding="utf-8") if brief_path else ""
    base_ref = y.trunk_ref(fetch=True)
    base = git("rev-parse", base_ref, cwd=y.root)
    if remote:
        trunk = y.cfg["trunk"]
        unpushed = git("rev-list", "--count", f"{base_ref}..refs/heads/{trunk}", cwd=y.root, check=False)
        if dirty:
            print(f"note: {checkout} has uncommitted changes ({len(dirty)} file(s)); lanes start from {base_ref} "
                  "without them. Commit and push them first if the lane needs them.")
        if unpushed and unpushed != "0":
            print(f"note: your {trunk} has {unpushed} commit(s) not on {base_ref}; lanes start without them. "
                  f"Push {trunk} first if the lane needs them.")
    git("worktree", "add", "--quiet", "-b", branch, str(wt), base, cwd=y.root)
    lane = Lane(name=name, branch=branch, worktree=str(wt), base=base, paths=paths, brief=brief,
                brief_path=brief_path, after=a.after or "", priority=a.priority)
    y.save(lane)
    y.log(name, f"new: {branch} from {base_ref} {base[:12]}, paths {', '.join(paths) or '-'}")
    print(f"lane {name}: {wt} on {branch} from {base_ref} {base[:9]}")
    if lane.after:
        print(f"  after {lane.after}: this lane joins a train only once {lane.after} has landed")
    y.lane_env("up", lane)
    for other, p, q in y.overlaps(lane):
        print(f"  overlap: {p} touches {q} in lane {other} (allowed; the train settles it)")
    return open_lane(y, lane, print_only=False) if a.open else 0


def open_command(y: Yard, lane: Lane) -> tuple[list[str] | None, str]:
    """The argv that opens a new terminal running the agent in this lane, and the same as a manual command."""
    wt, title = lane.worktree, f"lane {lane.name}"
    agent_cmd = y.agent_command(lane)
    agent = command_argv(agent_cmd)
    prompt = LANE_PROMPT.format(name=lane.name)
    manual = f'cd "{wt}" && {agent_cmd} "{prompt}"'
    if sys.platform == "win32":
        if shutil.which("wt"):
            return ["wt", "-w", "0", "new-tab", "--title", title, "-d", wt, "cmd", "/k", *agent, prompt], manual
        return ["cmd", "/c", "start", title, "/D", wt, "cmd", "/k", *agent, prompt], manual
    script = f"cd {shlex.quote(wt)} && {shlex.join(agent)} {shlex.quote(prompt)}"
    if sys.platform == "darwin":
        quoted = script.replace("\\", "\\\\").replace('"', '\\"')
        return ["osascript", "-e", f'tell application "Terminal" to do script "{quoted}"',
                "-e", 'tell application "Terminal" to activate'], manual
    if os.environ.get("TMUX"):
        return ["tmux", "new-window", "-n", lane.name, "-c", wt, f"{shlex.join(agent)} {shlex.quote(prompt)}"], manual
    for term in ("gnome-terminal", "konsole", "x-terminal-emulator"):
        if shutil.which(term):
            inner = f'{script}; exec "${{SHELL:-sh}}"'
            if term == "gnome-terminal":
                return [term, f"--working-directory={wt}", "--", "sh", "-c", inner], manual
            return [term, "-e", "sh", "-c", inner], manual
    return None, manual


def open_lane(y: Yard, lane: Lane, print_only: bool) -> int:
    argv, manual = open_command(y, lane)
    if print_only or argv is None:
        if argv is None:
            print("no supported terminal found; run this in a new terminal:")
        print(manual)
        return 0
    y.need_worktree(lane)
    subprocess.Popen(argv, cwd=lane.worktree)
    print(f"opened lane {lane.name} in a new terminal ({y.cfg['agent']})")
    return 0


def cmd_open(y: Yard, a) -> int:
    return open_lane(y, y.load(a.name), a.print)


def cmd_status(y: Yard, a) -> int:
    lanes = y.lanes()
    if not lanes:
        print("no lanes")
        return 0
    trunk = y.trunk_ref()
    cap = int(y.cfg["repair_attempts"])
    for lane in lanes:
        ahead = "?"
        if Path(lane.worktree).exists():
            ahead = git("rev-list", "--count", f"{trunk}..{lane.branch}", cwd=y.root, check=False) or "?"
        print(f"{lane.name:<20} {y.shown_state(lane):<10} {ahead:>3} ahead  {', '.join(lane.paths) or '-'}")
        facts = []
        if lane.priority:
            facts.append(f"priority {lane.priority}")
        if lane.after and lane.state != "landed":
            waiting = y.waiting_on(lane)
            facts.append(f"after {lane.after} ({'waiting' if waiting else 'landed'})")
        if lane.repairs:
            facts.append(f"repairs {lane.repairs}/{cap}")
        if y.parked(lane):
            facts.append(f"worktree missing: {lane.worktree}")
        if facts:
            print(f"{'':<20} {' · '.join(facts)}")
        for other, p, q in ([] if lane.state == "landed" else y.overlaps(lane)):
            print(f"{'':<20} overlap with {other}: {p} ~ {q}")
        if lane.note and lane.state in ("bounced", "active", "proposed"):
            print(f"{'':<20} {lane.note.splitlines()[0]}")
    count = followup_count(y)
    if count:
        print(f"\nfollow-ups: {count} in {y.followups_file()}")
    return 0


def cmd_show(y: Yard, a) -> int:
    lane = y.load(a.name)
    print(f"lane:     {lane.name} ({y.shown_state(lane)})")
    print(f"worktree: {lane.worktree}")
    print(f"branch:   {lane.branch}")
    print(f"paths:    {', '.join(lane.paths) or '-'}")
    if lane.after:
        print(f"after:    {lane.after} ({'waiting' if y.waiting_on(lane) else 'landed'})")
    if lane.priority:
        print(f"priority: {lane.priority}")
    for other, p, q in ([] if lane.state == "landed" else y.overlaps(lane)):
        print(f"overlap:  {p} ~ {q} in lane {other} (allowed; the train settles it)")
    brief = y.brief_text(lane)
    if brief:
        print("\n## Brief\n" + brief.strip())
    if lane.note:
        print("\n## Latest note\n" + lane.note.strip())
    if lane.state == "bounced" and y.notice_file(lane.name).exists():
        print(f"\nnotice: {y.notice_file(lane.name)}")
    return 0


def cmd_done(y: Yard, a) -> int:
    lane = y.load(a.name)
    if lane.state not in ("active", "bounced"):
        raise YardError(f"lane {lane.name} is {lane.state}")
    wt = y.need_worktree(lane)
    dirty = git("status", "--porcelain", "--untracked-files=all", cwd=wt).splitlines()
    if dirty:
        print(f"lane {lane.name} has uncommitted changes in {wt}:")
        print("\n".join(f"  {line}" for line in dirty))
        print("commit them (or remove them), then run pl done again")
        return 1
    base = y.trunk_ref(fetch=True)
    head = git("rev-parse", lane.branch, cwd=y.root)
    trunk_sha = git("rev-parse", base, cwd=y.root)
    if git("rev-list", "--count", f"{base}..{head}", cwd=y.root) == "0":
        raise YardError(f"lane {lane.name} has no commits beyond {base}")
    if lane.paths:
        # Scope is measured from the merge-base with the trunk, not the claim base: a lane that merged the
        # trunk must not be blamed for the trunk's own files.
        merge_base = git("merge-base", base, head, cwd=y.root)
        changed = [p for p in git("diff", "--name-only", "-z", merge_base, head, cwd=y.root).split("\0") if p]
        outside = [p for p in changed if not rules.in_claim(p, lane.paths) and not rules.is_generated(p, y.generated())]
        if outside and y.cfg["strict_paths"]:
            lane.head = head
            bounce(y, lane, "OUT_OF_SCOPE", f"{len(outside)} changed file(s) lie outside the lane's claim "
                                            f"({', '.join(lane.paths)}), and strict_paths is on.",
                   paths=outside, path_label="Files outside the claim",
                   fix="Move those changes out of this lane (revert them here), or ask for the lane to be "
                       "re-planned with a wider --paths claim.", repair=False, env_down=False)
            return 1
        if outside:
            print(f"warning: {len(outside)} file(s) outside the claim ({', '.join(lane.paths)}); "
                  "allowed, set strict_paths = true to bounce instead:")
            print("\n".join(f"  {p}" for p in outside))
            y.log(lane.name, f"outside the claim: {', '.join(outside)}")
    if lane.repairs and not os.environ.get("PLOWLINE_REPAIR"):
        print(f"repair count reset: pl done was run by hand, so {lane.name} gets "
              f"{y.cfg['repair_attempts']} fresh automatic repair(s)")
        lane.repairs = 0
    go, reason = (True, "review skipped") if a.no_review else y.review(lane, head, base)
    lane.note = reason
    if not go:
        lane.state = "active"
        y.save(lane)
        y.log(lane.name, f"NO-GO at {head[:12]}")
        print(f"NO-GO for {lane.name}:\n{tail(reason, 40)}")
        return 1
    lane.state, lane.head, lane.queued_at = "candidate", head, time.time()
    lane.reviewed_head, lane.reviewed_trunk = head, trunk_sha
    y.save(lane)
    y.log(lane.name, f"GO at {head[:12]} on trunk {trunk_sha[:12]}")
    added = record_followups(y, lane, head, reason)
    if added:
        print(f"follow-ups: {added} non-blocking finding(s) added to {y.followups_file()}")
    print(f"GO: {lane.name} {head[:9]} is a candidate for the next train")
    return 0


def settle_pr(y: Yard) -> bool:
    """PR mode: settle the last train's pull request. True while it is still open."""
    if not y.pr_file.exists():
        return False
    pr = json.loads(y.pr_file.read_text(encoding="utf-8"))
    info = json.loads(y.gh("pr", "view", pr["url"], "--json", "state,mergeCommit"))
    if info.get("state") == "OPEN":
        print(f"waiting on train PR {pr['url']}")
        return True
    lanes = [y.load(n) for n in pr["lanes"] if y.lane_file(n).exists()]
    if info.get("state") == "MERGED":
        commit = (info.get("mergeCommit") or {}).get("oid") or pr["head"]
        for lane in lanes:
            lane.state, lane.note, lane.repairs = "landed", f"landed in {commit} via {pr['url']}", 0
            y.save(lane)
            y.log(lane.name, f"landed in {commit} via {pr['url']}")
            y.events.append(("landed", lane.branch, ""))
            y.lane_env("down", lane)
        print(f"train PR merged: landed {len(lanes)} lane(s) at {commit[:9]}")
        y.after_land(lanes, commit, pr["url"])
    else:
        for lane in lanes:
            bounce(y, lane, "PR_CLOSED", f"train PR {pr['url']} was closed without merging.",
                   fix="Read the PR's comments and fix what they ask.", repair=False)
    y.pr_file.unlink()
    return False


def train_candidates(y: Yard) -> list[Lane]:
    """Candidates in train order (priority, then queue time), minus a stale GO and lanes still waiting --after."""
    out = []
    for lane in sorted((l for l in y.lanes() if l.state == "candidate"), key=lambda l: (-l.priority, l.queued_at)):
        bound = lane.reviewed_head or lane.head
        current = git("rev-parse", "--verify", "-q", f"refs/heads/{lane.branch}", cwd=y.root, check=False)
        if current and current != bound:
            lane.state = "active"
            lane.note = (f"STALE_REVIEW: {lane.branch} moved from {bound[:9]} to {current[:9]} after its GO, and a "
                         f"GO is bound to the commit it reviewed. Run `pl done {lane.name}` to review the new head.")
            y.save(lane)
            y.log(lane.name, lane.note)
            print(f"not in this train: {lane.note}")
            continue
        waiting = y.waiting_on(lane)
        if waiting:
            print(f"not in this train: {lane.name} waits for {waiting} to land")
            continue
        out.append(lane)
    return out


def merge_lane(y: Yard, tree: Path, lane: Lane) -> tuple[bool, list[str]]:
    """Merge one lane into the scratch tree. Returns (merged, conflicting paths).

    With git 2.38+ a `git merge-tree` preview finds a conflict without touching the tree. A conflict wholly in
    `generated` files (with `regenerate` set) takes the trunk's side; regenerate rebuilds them afterwards."""
    gen = y.generated()
    can_regen = bool(str(y.cfg["regenerate"]).strip()) and bool(gen)
    message = f"plowline: land {lane.branch}"
    if y.merge_tree_ok():
        p = git_run("merge-tree", "--write-tree", "--name-only", "HEAD", lane.head, cwd=tree)
        if p.returncode == 1:
            paths = rules.merge_tree_paths(p.stdout)
            if not (paths and can_regen and all(rules.is_generated(x, gen) for x in paths)):
                return False, paths or ["(git merge-tree named no paths)"]
        elif p.returncode != 0:
            raise YardError(f"git merge-tree failed on {lane.branch} (exit {p.returncode}):\n"
                            f"{(p.stderr or p.stdout).strip()}")
    p = git_run("merge", "--no-ff", "--no-edit", "-m", message, lane.head, cwd=tree)
    if p.returncode == 0:
        return True, []
    unmerged = [x for x in git("diff", "--name-only", "--diff-filter=U", cwd=tree, check=False).splitlines() if x]
    if not unmerged:
        git_run("merge", "--abort", cwd=tree)
        raise YardError(f"git merge of {lane.branch} failed without a conflict (exit {p.returncode}); "
                        f"candidates are unchanged:\n{(p.stderr or p.stdout).strip()}")
    if can_regen and all(rules.is_generated(x, gen) for x in unmerged):
        ok = (git_run("checkout", "--ours", "--", *unmerged, cwd=tree).returncode == 0
              and git_run("add", "--", *unmerged, cwd=tree).returncode == 0
              and not git("diff", "--name-only", "--diff-filter=U", cwd=tree, check=False)
              and git_run("commit", "--quiet", "--no-edit", "-m",
                          f"{message} (generated: {', '.join(unmerged)} from the trunk, then regenerated)",
                          cwd=tree).returncode == 0)
        if ok:
            print(f"generated conflict in {', '.join(unmerged)} ({lane.name}): took the trunk's side; "
                  "regenerate rebuilds it")
            return True, []
    git_run("merge", "--abort", cwd=tree)
    return False, unmerged


class GateUnavailable(Exception):
    pass


def cmd_train(y: Yard, a) -> int:
    if a.full_steam:
        return full_steam(y, a)
    return run_train(y, dry_run=a.dry_run)


def run_train(y: Yard, dry_run: bool = False, stale_after: float | None = None) -> int:
    """One train: merge the candidates, gate (bisecting on red), land once, bounce the rest."""
    steps = y.gate_steps()
    if not steps:
        raise YardError("set `gate` in plowline.toml first")
    if y.cfg["land"] not in ("push", "pr"):
        raise YardError(f"land must be \"push\" or \"pr\", not {y.cfg['land']!r}")
    pr_mode = y.cfg["land"] == "pr"
    if pr_mode and not y.has_remote():
        raise YardError("land = \"pr\" needs a remote")
    with y.lock(stale_after=stale_after):
        if pr_mode and settle_pr(y):
            return 0
        cands = train_candidates(y)
        if not cands:
            print("no candidates")
            return 0
        base_ref = y.trunk_ref(fetch=True)
        tip = git("rev-parse", base_ref, cwd=y.root)
        for lane in cands:
            if lane.reviewed_trunk and lane.reviewed_trunk != tip:
                print(f"{lane.name}: {base_ref} moved since its review ({lane.reviewed_trunk[:9]} -> {tip[:9]}); "
                      "the train gates the merged result")
        scratch = Path(tempfile.mkdtemp(prefix="plowline-train-"))
        shutil.rmtree(scratch)
        git("worktree", "add", "--quiet", "--detach", str(scratch), tip, cwd=y.root)
        bounced = 0
        try:
            merged: list[Lane] = []
            for lane in cands:
                ok, paths = merge_lane(y, scratch, lane)
                if ok:
                    merged.append(lane)
                    continue
                against = f"{base_ref} {tip[:9]}" + (
                    f" + lanes {', '.join(l.name for l in merged)} from this train" if merged else "")
                hint = (" Every conflicting file is listed in `generated`; set `regenerate` so the train can "
                        "rebuild them instead." if y.generated() and not str(y.cfg["regenerate"]).strip()
                        and all(rules.is_generated(p, y.generated()) for p in paths) else "")
                bounce(y, lane, "MERGE_CONFLICT", f"{lane.branch} conflicts with {against}.{hint}", paths=paths,
                       fix="Resolve the conflicting paths in the merge, keeping both sides' intent.")
                bounced += 1
            if not merged:
                print("nothing landed")
                return 1
            green = gate_train(y, scratch, tip, base_ref, merged, steps)
            bounced += len(merged) - len(green)
            if not green:
                print("nothing landed")
                return 1
            head = git("rev-parse", "HEAD", cwd=scratch)
            if dry_run:
                print(f"dry run: would {'open a PR for' if pr_mode else 'push'} {head[:9]} to {base_ref}")
                return 0
            if pr_mode:
                open_train_pr(y, green, head)
                return 1 if bounced else 0
            if y.has_remote():
                p = git_run("push", "--quiet", y.cfg["remote"], f"HEAD:refs/heads/{y.cfg['trunk']}", cwd=scratch)
                if p.returncode != 0:
                    raise YardError(f"push refused (exit {p.returncode}; did {base_ref} move?); candidates are "
                                    f"unchanged, run the train again\n{p.stderr.strip()}")
            else:
                y.land_local(head, tip)
            for lane in green:
                lane.state, lane.note, lane.repairs = "landed", f"landed in {head}", 0
                y.save(lane)
                y.log(lane.name, f"landed in {head}")
                y.events.append(("landed", lane.branch, ""))
                y.lane_env("down", lane)
            print(f"landed {len(green)} lane(s) on {base_ref} at {head[:9]}")
            hook_ok = y.after_land(green, head)
            return 1 if bounced or not hook_ok else 0
        finally:
            git("worktree", "remove", "--force", str(scratch), cwd=y.root, check=False)


def gate_train(y: Yard, scratch: Path, tip: str, base_ref: str, merged: list[Lane], steps: list[str]) -> list[Lane]:
    """Gate the merged train; on red, split it until the merged set is green. Bounces the lanes that turned it
    red, leaves the scratch tree at the green set's head, and returns the green lanes."""
    heads: dict[tuple[str, ...], str] = {(): tip, tuple(l.name for l in merged): git("rev-parse", "HEAD", cwd=scratch)}
    results: dict[tuple[str, ...], gates.GateResult] = {}
    built = [tuple(l.name for l in merged)]  # the tree currently checked out holds this set, before regenerate
    merge_failures: dict[tuple[str, ...], tuple[str, list[str]]] = {}  # subset -> (lane that failed, paths)

    def build(subset: list[Lane]) -> bool:
        key = tuple(l.name for l in subset)
        if built[0] != key:
            git("reset", "--quiet", "--hard", tip, cwd=scratch)
            git("clean", "-fdq", cwd=scratch)
            for lane in subset:
                ok, paths = merge_lane(y, scratch, lane)
                if not ok:  # merged in the full train but not in this smaller set: count it as red
                    merge_failures[key] = (lane.name, paths)
                    git("reset", "--quiet", "--hard", tip, cwd=scratch)
                    built[0] = ()
                    return False
            built[0] = key
        if str(y.cfg["regenerate"]).strip():
            ok, out = shell(y.cfg["regenerate"], scratch)
            if not ok:
                raise YardError(f"regenerate failed on the merged tree; candidates are unchanged:\n{tail(out)}")
            if git("status", "--porcelain", cwd=scratch):
                git("add", "-A", cwd=scratch)
                git("commit", "--quiet", "-m", "plowline: regenerate on the merged tree", cwd=scratch)
                built[0] = None  # the tree now carries a regenerate commit: rebuild before reusing it
        heads[key] = git("rev-parse", "HEAD", cwd=scratch)
        return True

    def gate(subset: list[Lane]) -> bool:
        key = tuple(l.name for l in subset)
        names = ", ".join(key) or "no lanes (the trunk alone)"
        if not build(subset):
            failed, paths = merge_failures[key]
            print(f"gate on {len(key)} lane(s): {names} -> {failed} does not merge in this smaller set "
                  f"({', '.join(paths)}); counted as red")
            return False
        result = gates.run_gate(steps, gates.GateContext(tree=scratch, base=tip, head=heads[key], lanes=list(key)))
        results[key] = result
        print(f"gate on {len(key)} lane(s): {names} -> {result.line}")
        if result.status == "unavailable":
            raise GateUnavailable(result)
        return result.ok

    blamed: dict[str, tuple[str, ...]] = {}
    try:
        green, culprits = rules.find_green(
            merged, gate, on_blame=lambda lane, accepted: blamed.__setitem__(lane.name, tuple(l.name for l in accepted)))
    except GateUnavailable as e:
        result = e.args[0]
        raise YardError(f"{result.line}\nThe gate could not run, so no lane is blamed and the candidates are "
                        f"unchanged. Fix the gate command in plowline.toml.\n{tail(result.output)}")
    except rules.TrunkRed:
        result = results.get((), gates.GateResult("fail", ""))
        raise YardError(f"GATE_RED_ON_TRUNK: the gate is red on {base_ref} {tip[:9]} with no lane merged, so no "
                        f"lane is blamed and the candidates are unchanged. Fix the trunk first.\n{result.line}\n"
                        f"{tail(result.output)}")
    if culprits:
        print(f"bisection: red {', '.join(l.name for l in culprits)}; green {', '.join(l.name for l in green) or '-'} "
              f"({len(results)} gate runs)")
    for lane in culprits:
        accepted = blamed[lane.name]
        result = results.get(accepted + (lane.name,))
        on_top = f"{base_ref} {tip[:9]}" + (f" + {', '.join(accepted)}" if accepted else "")
        failed = merge_failures.get(accepted + (lane.name,))
        if failed and failed[0] == lane.name:
            bounce(y, lane, "MERGE_CONFLICT", f"{lane.branch} merged with the whole train, but it conflicts with "
                                              f"{on_top} once the red lanes are left out.", paths=failed[1],
                   fix="Resolve the conflicting paths in the merge, keeping both sides' intent; the lane may "
                       "lean on work from a lane that bounced.")
            continue
        bounce(y, lane, "GATE_FAIL", f"the gate went red when {lane.name} was merged onto {on_top}, which is green "
                                     f"without it.\n{result.line if result else ''}",
               output=result.output if result else "",
               fix="Reproduce the failure: run the gate in your worktree after the merge, fix it, and add a test.")
    if green:
        key = tuple(l.name for l in green)
        git("reset", "--quiet", "--hard", heads[key], cwd=scratch)
    return green


def open_train_pr(y: Yard, lanes: list[Lane], head: str) -> None:
    """PR mode: push the gated train to its own branch and open one pull request for it."""
    branch = f"plowline/train-{time.strftime('%Y%m%d-%H%M%S')}-{head[:7]}"
    p = git_run("push", "--quiet", y.cfg["remote"], f"{head}:refs/heads/{branch}", cwd=y.root)
    if p.returncode != 0:
        raise YardError(f"push of {branch} refused (exit {p.returncode}); candidates are unchanged\n{p.stderr.strip()}")
    body = "Lanes in this train, merged onto the trunk and gated together:\n\n" + "".join(
        f"- `{l.branch}`: {(y.brief_text(l).strip().splitlines() or ['(no brief)'])[0].lstrip('# ')}\n"
        for l in lanes) + (
        f"\nGate passed on the merged tree: {', '.join(f'`{s}`' for s in y.gate_steps())}\n\n"
        "Merge it with any method. `pl train` or `pl watch` notices the merge and marks the lanes landed.\n")
    out = y.gh("pr", "create", "--base", y.cfg["trunk"], "--head", branch,
               "--title", f"plowline: land {', '.join(l.name for l in lanes)}", "--body-file", "-", stdin=body)
    url = out.splitlines()[-1].strip() if out else branch
    for lane in lanes:
        lane.state, lane.note = "proposed", f"in train PR {url}"
        y.save(lane)
    y.pr_file.write_text(json.dumps({"url": url, "branch": branch, "head": head,
                                     "lanes": [l.name for l in lanes]}, indent=2), encoding="utf-8")
    print(f"opened train PR {url} for {len(lanes)} lane(s); the next train waits for it")


def cmd_resume(y: Yard, a) -> int:
    lane = y.load(a.name)
    if lane.state != "bounced":
        raise YardError(f"lane {lane.name} is {lane.state}, not bounced")
    y.need_worktree(lane)
    lane.state = "active"
    y.save(lane)
    y.log(lane.name, "resumed")
    y.lane_env("up", lane)
    trunk = y.trunk_ref(fetch=True)
    print(f"lane {lane.name} is active again in {lane.worktree}\n")
    notice = y.notice_file(lane.name)
    if notice.exists():
        print(notice.read_text(encoding="utf-8").strip())
        print(f"\n(notice: {notice})")
    else:
        print(lane.note.strip())
        print(f"\nnext:\n  git -C \"{lane.worktree}\" merge {trunk}   # merge, never rebase\n"
              f"  fix, test, commit\n  pl done {lane.name}")
    return 0


def backup_env_files(y: Yard, lane: Lane, wt: Path) -> list[str]:
    """Copy a lane's untracked .env* files (ignored ones included) to .plowline/backups/<lane>/, then remove them."""
    found = [p for p in git("ls-files", "-z", "--others", "--", ":(glob)**/.env*", cwd=wt).split("\0") if p]
    dest = y.notes / "backups" / lane.name
    for rel in found:
        target = dest / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(wt / rel, target)
        (wt / rel).unlink()
    return found


def cmd_clean(y: Yard, a) -> int:
    trunk = y.trunk_ref(fetch=y.has_remote())
    git("worktree", "prune", cwd=y.root, check=False)
    for lane in y.lanes():
        if lane.state != "landed":
            continue
        wt = Path(lane.worktree)
        if wt.exists():
            dirty = [line for line in git("status", "--porcelain", "--untracked-files=all", cwd=wt,
                                          check=False).splitlines()
                     if not (line.startswith("?? ") and Path(line[3:].strip('"')).name.startswith(".env"))]
            if dirty:
                print(f"kept {lane.name}: {wt} has uncommitted changes:")
                print("\n".join(f"  {line}" for line in dirty))
                continue
            saved = backup_env_files(y, lane, wt)
            if saved:
                print(f"backed up {', '.join(saved)} from {lane.name} to {y.notes / 'backups' / lane.name}")
            git("worktree", "remove", str(wt), cwd=y.root)
        if git_ok("merge-base", "--is-ancestor", lane.branch, trunk, cwd=y.root):
            git("branch", "-D", lane.branch, cwd=y.root, check=False)
        y.lane_file(lane.name).unlink()
        y.log(lane.name, "cleaned (worktree and branch removed; this log and the notices stay)")
        print(f"cleaned {lane.name}")
    return 0


REASONS = {"GATE_FAIL": "gate red", "MERGE_CONFLICT": "conflict", "OUT_OF_SCOPE": "out of scope",
           "PR_CLOSED": "PR closed"}
FULL_STEAM_FAILURES = 5  # consecutive failed cycles before the loop stops
HEARTBEAT_SECONDS = 600


def train_ready(y: Yard) -> bool:
    """A lane is done and free to ride: a candidate not waiting on --after, or a proposed lane in PR mode."""
    return any(l.state == "proposed" or (l.state == "candidate" and not y.waiting_on(l)) for l in y.lanes())


def lane_counts(y: Yard) -> dict[str, int]:
    """Lanes still in play: working (active, done or proposed) and bounced. Landed and parked lanes don't count."""
    counts = {"working": 0, "bounced": 0}
    for lane in y.lanes():
        if lane.state == "landed" or y.parked(lane):
            continue
        counts["bounced" if lane.state == "bounced" else "working"] += 1
    return counts


def train_cycle(y: Yard, stale_after: float) -> list[str]:
    """One full-steam cycle: a train whose own output goes to .plowline/logs/train.log. Returns the lines to
    print (any stale-lock notice, then the summary). Raises on a failed cycle; LockBusy while another train runs."""
    import io
    from contextlib import redirect_stdout
    y.events.clear()
    buf = io.StringIO()
    try:
        with redirect_stdout(buf):
            run_train(y, stale_after=stale_after)
    finally:
        log = y.notes / "logs" / "train.log"
        log.parent.mkdir(parents=True, exist_ok=True)
        with log.open("a", encoding="utf-8") as f:
            f.write(f"--- cycle {time.strftime('%Y-%m-%d %H:%M:%S')}\n{buf.getvalue()}")
    notes = [line for line in buf.getvalue().splitlines() if line.startswith("removed a stale")]
    landed = [e for e in y.events if e[0] == "landed"]
    bounced = [e for e in y.events if e[0] == "bounced"]
    why = f" ({', '.join(f'{b}: {REASONS.get(r, r.lower())}' for _, b, r in bounced)})" if bounced else ""
    working = lane_counts(y)["working"]
    return notes + [f"{len(landed)} landed, {len(bounced)} bounced{why}, {working} working"]


def full_steam(y: Yard, a) -> int:
    """`pl train --full-steam`: check every --every seconds and run one train whenever lanes are done.

    One line per cycle and a heartbeat every ten minutes when idle. Ctrl-C while waiting stops at once; Ctrl-C
    during a cycle lets that cycle finish its merge, landing or rollback first, so the trunk is never left
    half-landed. A failed cycle is logged to .plowline/logs/train.log; five in a row stop the loop."""
    import signal
    import traceback
    every, timeout = max(1, a.every), max(1, a.cycle_timeout)

    def stamp() -> str:
        return time.strftime("%H:%M:%S")

    with y.lock("full-steam.lock", "full-steam loop", stale_after=timeout) as lock_path:
        print(f"full steam: a train whenever lanes are done, checked every {every}s. Ctrl-C to stop.")
        failures, last_line = 0, time.monotonic()
        gates.isolate_children(True)
        try:
            while True:
                if train_ready(y):
                    interrupted: list[bool] = []
                    previous = signal.signal(signal.SIGINT, lambda *_: interrupted.append(True))
                    try:
                        lines = train_cycle(y, timeout)
                    except LockBusy as e:
                        print(f"{stamp()}  waiting: {e}")
                    except Exception as e:  # a crash in one cycle must not stop the loop
                        failures += 1
                        log = y.notes / "logs" / "train.log"
                        log.parent.mkdir(parents=True, exist_ok=True)
                        with log.open("a", encoding="utf-8") as f:
                            f.write(f"cycle failed ({failures}/{FULL_STEAM_FAILURES}):\n{traceback.format_exc()}\n")
                        first = (str(e).strip().splitlines() or [type(e).__name__])[0]
                        print(f"{stamp()}  cycle failed ({failures}/{FULL_STEAM_FAILURES}): {first}")
                        if failures >= FULL_STEAM_FAILURES:
                            print(f"full steam stopped after {failures} failed cycles in a row (log: {log}). "
                                  f"Last error: {type(e).__name__}: {str(e).strip()}")
                            return 2
                    else:
                        failures = 0
                        for note in lines[:-1]:
                            print(f"{stamp()}  {note}")
                        print(f"{stamp()}  {lines[-1]}, next check in {every}s")
                    finally:
                        signal.signal(signal.SIGINT, previous)
                    last_line = time.monotonic()
                    if interrupted:
                        print("full steam stopped (Ctrl-C) after finishing the cycle")
                        return 0
                elif time.monotonic() - last_line >= HEARTBEAT_SECONDS:
                    counts = lane_counts(y)
                    print(f"{stamp()}  idle: {counts['working']} working, {counts['bounced']} bounced, "
                          f"next check in {every}s")
                    last_line = time.monotonic()
                if a.until_empty and not any(lane_counts(y).values()):
                    print(f"{stamp()}  no lane is working, done or bounced: full steam stopped")
                    return 0
                os.utime(lock_path)  # a live loop's lock never looks stale
                try:
                    time.sleep(every)
                except KeyboardInterrupt:
                    print("full steam stopped")
                    return 0
        finally:
            gates.isolate_children(False)


def cmd_watch(y: Yard, a) -> int:
    """A control tower for one terminal: lane status, and a train whenever a lane becomes a candidate."""
    import io
    from contextlib import redirect_stdout
    events: list[str] = []
    try:
        while True:
            ready = any(l.state == "proposed" or (l.state == "candidate" and not y.waiting_on(l)) for l in y.lanes())
            if ready and not a.no_train:
                buf = io.StringIO()
                with redirect_stdout(buf):
                    try:
                        run_train(y)
                    except YardError as e:
                        print(f"train stopped: {e}")
                stamp = time.strftime("%H:%M:%S")
                events += [f"{stamp}  {line}" for line in buf.getvalue().splitlines() if line.strip()]
                events = events[-12:]
            if sys.stdout.isatty() and not a.once:
                print("\033[2J\033[H", end="")
            print(f"plowline watch · {time.strftime('%H:%M:%S')} · every {a.interval}s · Ctrl-C to stop\n")
            cmd_status(y, a)
            if events:
                print("\nrecent trains:")
                print("\n".join(events))
            if a.once:
                return 0
            time.sleep(a.interval)
    except KeyboardInterrupt:
        return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="pl", description="Plowline: run many coding agents on one repo.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for cmd in ("setup", "init"):
        p = sub.add_parser(cmd, help="set up this repo (wizard; --yes for agents)" if cmd == "setup" else "alias for setup")
        p.add_argument("--yes", action="store_true", help="no prompts; take detected defaults and flags")
        p.add_argument("--gate", help="command that must pass before a push")
        p.add_argument("--review", help="builtin:claude, builtin:codex, a command, or '' for none")
        p.add_argument("--regenerate", help="command that rebuilds generated files")
        p.add_argument("--trunk")
        p.add_argument("--remote", help="remote the train pushes to; '' for local-only")
        p.add_argument("--agent", help="command `pl open` starts in a lane (default: the first agent CLI found on PATH)")
        p.add_argument("--land", choices=("push", "pr"), help="push to the trunk (default) or open a PR per train")
        p.add_argument("--on-land", help="command run after each landing (deploy, tag, notify)")
        p.add_argument("--force", action="store_true", help="rewrite an existing plowline.toml")
        p.add_argument("--skip-gate-check", action="store_true")
        p.add_argument("--no-agent-files", action="store_true", help="leave AGENTS.md, CLAUDE.md, briefs/ and .claude/skills/ alone")
        p.add_argument("--commit", action="store_true", help="commit the setup (unattended mode)")
        p.add_argument("--push", action="store_true", help="push the setup commit (unattended mode)")
    p = sub.add_parser("new", help="start a lane: worktree + branch + claim")
    p.add_argument("name")
    p.add_argument("--paths", nargs="*", help="files or globs this lane expects to touch")
    p.add_argument("--brief", nargs="?", const=True,
                   help="file holding the lane's brief; with no file, write briefs/<lane>.md to fill in")
    p.add_argument("--after", help="a lane that must land before this one can join a train")
    p.add_argument("--priority", type=int, default=0, help="higher joins the train first (default 0)")
    p.add_argument("--open", action="store_true", help="also open a terminal with the agent in the lane")
    p = sub.add_parser("open", help="open a new terminal with the agent running in a lane")
    p.add_argument("name")
    p.add_argument("--print", action="store_true", help="print the command instead of opening a terminal")
    sub.add_parser("status", help="list lanes, claims, overlaps and follow-ups")
    p = sub.add_parser("watch", help="control tower: refresh status, run a train when a lane is done")
    p.add_argument("--interval", type=int, default=15, help="seconds between refreshes")
    p.add_argument("--no-train", action="store_true", help="only show status")
    p.add_argument("--once", action="store_true", help="one refresh, then exit")
    p = sub.add_parser("show", help="print a lane's brief and latest note")
    p.add_argument("name")
    p = sub.add_parser("done", help="check scope, review a lane and make it a train candidate")
    p.add_argument("name")
    p.add_argument("--no-review", action="store_true")
    p = sub.add_parser("train", help="merge candidates onto the trunk, gate once, push once")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--full-steam", "--watch", dest="full_steam", action="store_true",
                   help="keep going: run a train whenever lanes are done, until Ctrl-C")
    p.add_argument("--every", type=int, default=30, help="full steam: seconds between checks (default 30)")
    p.add_argument("--until-empty", action="store_true",
                   help="full steam: stop once no lane is working, done or bounced")
    p.add_argument("--cycle-timeout", type=int, default=LOCK_STALE_SECONDS,
                   help="full steam: a lock older than this many seconds is stale (default 7200)")
    p = sub.add_parser("resume", help="reopen a bounced lane and print its notice")
    p.add_argument("name")
    sub.add_parser("clean", help="remove worktrees and branches of landed lanes")
    p = sub.add_parser("demo", help="five lanes and two trains in a throwaway repo")
    p.add_argument("--keep", action="store_true", help="keep the throwaway repo to look around")
    a = ap.parse_args(argv)
    if a.cmd == "demo":
        from plowline import demo
        result = demo.run()
        if a.keep:
            print(f"kept {result['tmp']}")
        else:
            shutil.rmtree(result["tmp"], ignore_errors=True)
        return 0
    try:
        y = Yard(Path.cwd())
        return {"setup": cmd_setup, "init": cmd_setup, "new": cmd_new, "status": cmd_status, "show": cmd_show,
                "done": cmd_done, "train": cmd_train, "resume": cmd_resume, "open": cmd_open,
                "watch": cmd_watch, "clean": cmd_clean}[a.cmd](y, a)
    except YardError as e:
        print(f"plowline: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
