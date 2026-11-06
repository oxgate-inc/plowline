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

CONFIG_NAME = "plowline.toml"
DEFAULTS = {
    "trunk": "main",
    "remote": "origin",
    "gate": "",
    "review": "",
    "regenerate": "",
    "worktrees": "../{repo}-lanes",
    "branch_prefix": "lane/",
    "agent": "claude",
    "land": "push",
    "on_land": "",
    "gh": "gh",
}
LANE_NAME = re.compile(r"^[a-z0-9][a-z0-9._-]{0,62}$")
DIFF_LIMIT = 200_000
LOCK_STALE_SECONDS = 2 * 60 * 60
LANE_PROMPT = ("You are working in the Plowline lane {name}. Run pl show {name} to read your brief, "
               "and follow the lane rules in CLAUDE.md or AGENTS.md. When your work is committed and the tests "
               "pass, run pl done {name}. If it answers NO-GO, fix the findings and run it again.")

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
    state: str = "active"  # active | candidate | proposed | bounced | landed
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
        self.pr_file = self.state_dir / "train-pr.json"
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

    def land_local(self, head: str, tip: str) -> None:
        """No remote: move the local trunk to the train's head, updating its checkout if it has one."""
        trunk = self.cfg["trunk"]
        for block in git("worktree", "list", "--porcelain", cwd=self.root).split("\n\n"):
            lines = block.splitlines()
            if f"branch refs/heads/{trunk}" in lines:
                path = Path(lines[0].removeprefix("worktree "))
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
    version = tuple(int(n) for n in re.findall(r"\d+", git("--version", cwd=y.root))[:2])
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
        agent = ask("Command that starts an agent in a lane (used by pl open)", a.agent or "claude", interactive)
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
            f"gate = {json.dumps(gate)}  # must pass on the merged train before anything is pushed\n"
            f"review = {json.dumps(review)}  # builtin:claude, builtin:codex, any command (exit 0 = GO), or \"\"\n"
            f"regenerate = {json.dumps(regenerate)}  # optional: rebuilds generated files on the merged tree\n"
            f"agent = {json.dumps(agent)}  # what `pl open` starts in a lane's terminal\n"
            f"land = {json.dumps(land)}  # \"push\" to the trunk, or \"pr\": open one pull request per train\n"
            f"on_land = {json.dumps(on_land)}  # optional: runs after each landing (deploy, tag, notify)\n"
            'worktrees = "../{repo}-lanes"\n',
            encoding="utf-8")
        y.cfg.update(trunk=trunk, remote=remote, gate=gate, review=review, regenerate=regenerate, agent=agent,
                     land=land, on_land=on_land)
        say(f"wrote {CONFIG_NAME}")

    if not a.skip_gate_check:
        say(f"checking the gate on this checkout: {y.cfg['gate']}")
        ok, out = shell(y.cfg["gate"], y.root)
        if not ok:
            raise YardError(f"the gate fails before any lane exists; fix the trunk first\n{tail(out)}")
        say("gate passes")

    touched = [CONFIG_NAME]
    if not a.no_agent_files:
        rules = template("AGENTS-snippet.md")
        for name in ("AGENTS.md", "CLAUDE.md"):
            f = y.root / name
            text = f.read_text(encoding="utf-8") if f.exists() else ""
            if AGENT_RULES_HEADING not in text:
                f.write_text((text.rstrip() + "\n\n" if text.strip() else "") + rules, encoding="utf-8")
                say(f"added lane rules to {name}")
                touched.append(name)
        brief = y.root / "briefs" / "TEMPLATE.md"
        if not brief.exists():
            brief.parent.mkdir(exist_ok=True)
            brief.write_text(template("lane-brief.md"), encoding="utf-8")
            say("wrote briefs/TEMPLATE.md")
            touched.append("briefs/TEMPLATE.md")

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
    say("\nnext:\n  pl new <lane> --paths <dirs or files> --brief briefs/<lane>.md --open\n"
        "  pl watch     # in this terminal: status, and a train whenever a lane is done")
    return 0


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
    return open_lane(y, lane, print_only=False) if a.open else 0


def open_command(y: Yard, lane: Lane) -> tuple[list[str] | None, str]:
    """The argv that opens a new terminal running the agent in this lane, and the same as a manual command."""
    wt, title = lane.worktree, f"lane {lane.name}"
    agent = shlex.split(str(y.cfg["agent"]))
    prompt = LANE_PROMPT.format(name=lane.name)
    manual = f'cd "{wt}" && {y.cfg["agent"]} "{prompt}"'
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
    for lane in lanes:
        ahead = "?"
        if Path(lane.worktree).exists():
            ahead = git("rev-list", "--count", f"{trunk}..{lane.branch}", cwd=y.root, check=False) or "?"
        print(f"{lane.name:<20} {lane.state:<10} {ahead:>3} ahead  {', '.join(lane.paths) or '-'}")
        for other, p, q in ([] if lane.state == "landed" else y.overlaps(lane)):
            print(f"{'':<20} overlap with {other}: {p} ~ {q}")
        if lane.note and lane.state in ("bounced", "active", "proposed"):
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
            lane.state, lane.note = "landed", f"landed in {commit} via {pr['url']}"
            y.save(lane)
        print(f"train PR merged: landed {len(lanes)} lane(s) at {commit[:9]}")
        y.after_land(lanes, commit, pr["url"])
    else:
        for lane in lanes:
            bounce(y, lane, f"PR_CLOSED: train PR {pr['url']} was closed without merging.\n"
                            f"Read the PR, fix what it asks, then `pl done {lane.name}` to queue the lane again.")
    y.pr_file.unlink()
    return False


def cmd_train(y: Yard, a) -> int:
    if not str(y.cfg["gate"]).strip():
        raise YardError("set `gate` in plowline.toml first")
    if y.cfg["land"] not in ("push", "pr"):
        raise YardError(f"land must be \"push\" or \"pr\", not {y.cfg['land']!r}")
    pr_mode = y.cfg["land"] == "pr"
    if pr_mode and not y.has_remote():
        raise YardError("land = \"pr\" needs a remote")
    with y.lock():
        if pr_mode and settle_pr(y):
            return 0
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
                    against = f"{base_ref} {tip[:9]}" + (
                        f" + lanes {', '.join(l.name for l, _ in merged)} from this train" if merged else "")
                    bounce(y, lane, f"MERGE_CONFLICT with {against}: "
                                    f"{', '.join(paths.split()) or 'see git output'}\n"
                                    f"Merge {base_ref} into {lane.branch}, resolve, re-test, then `pl done {lane.name}`.")
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
                                f"{tail(out)}\nMerge {base_ref} into {lane.branch}, fix, re-test, then `pl done {lane.name}`.")
                bounced += 1
            if not merged:
                print("nothing landed")
                return 1
            head = git("rev-parse", "HEAD", cwd=scratch)
            if a.dry_run:
                print(f"dry run: would {'open a PR for' if pr_mode else 'push'} {head[:9]} to {base_ref}")
                return 0
            if pr_mode:
                open_train_pr(y, merged, head)
                return 1 if bounced else 0
            if y.has_remote():
                p = subprocess.run(["git", "push", "--quiet", y.cfg["remote"], f"HEAD:refs/heads/{y.cfg['trunk']}"],
                                   cwd=scratch, capture_output=True, text=True)
                if p.returncode != 0:
                    raise YardError(f"push refused (did {base_ref} move?); candidates are unchanged, run the train again\n"
                                    f"{p.stderr.strip()}")
            else:
                y.land_local(head, tip)
            for lane, _ in merged:
                lane.state, lane.note = "landed", f"landed in {head}"
                y.save(lane)
            print(f"landed {len(merged)} lane(s) on {base_ref} at {head[:9]}")
            hook_ok = y.after_land([l for l, _ in merged], head)
            return 1 if bounced or not hook_ok else 0
        finally:
            git("worktree", "remove", "--force", str(scratch), cwd=y.root, check=False)


def open_train_pr(y: Yard, merged: list[tuple[Lane, str]], head: str) -> None:
    """PR mode: push the gated train to its own branch and open one pull request for it."""
    branch = f"plowline/train-{time.strftime('%Y%m%d-%H%M%S')}"
    p = subprocess.run(["git", "push", "--quiet", y.cfg["remote"], f"{head}:refs/heads/{branch}"],
                       cwd=y.root, capture_output=True, text=True)
    if p.returncode != 0:
        raise YardError(f"push of {branch} refused; candidates are unchanged\n{p.stderr.strip()}")
    lanes = [l for l, _ in merged]
    body = "Lanes in this train, merged onto the trunk and gated together:\n\n" + "".join(
        f"- `{l.branch}`: {(l.brief.strip().splitlines() or ['(no brief)'])[0].lstrip('# ')}\n" for l in lanes) + (
        f"\nGate passed on the merged tree: `{y.cfg['gate']}`\n\n"
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
    lane.state = "active"
    y.save(lane)
    trunk = y.trunk_ref(fetch=True)
    print(f"lane {lane.name} is active again in {lane.worktree}\n")
    print(lane.note.strip())
    print(f"\nnext:\n  git -C \"{lane.worktree}\" merge {trunk}   # merge, never rebase\n"
          f"  fix, test, commit\n  pl done {lane.name}")
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


def cmd_watch(y: Yard, a) -> int:
    """A control tower for one terminal: lane status, and a train whenever a lane becomes a candidate."""
    import io
    from contextlib import redirect_stdout
    events: list[str] = []
    try:
        while True:
            if any(l.state in ("candidate", "proposed") for l in y.lanes()) and not a.no_train:
                buf = io.StringIO()
                with redirect_stdout(buf):
                    try:
                        cmd_train(y, argparse.Namespace(dry_run=False))
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
        p.add_argument("--agent", help="command `pl open` starts in a lane (default: claude)")
        p.add_argument("--land", choices=("push", "pr"), help="push to the trunk (default) or open a PR per train")
        p.add_argument("--on-land", help="command run after each landing (deploy, tag, notify)")
        p.add_argument("--force", action="store_true", help="rewrite an existing plowline.toml")
        p.add_argument("--skip-gate-check", action="store_true")
        p.add_argument("--no-agent-files", action="store_true", help="leave AGENTS.md, CLAUDE.md and briefs/ alone")
        p.add_argument("--commit", action="store_true", help="commit the setup (unattended mode)")
        p.add_argument("--push", action="store_true", help="push the setup commit (unattended mode)")
    p = sub.add_parser("new", help="start a lane: worktree + branch + claim")
    p.add_argument("name")
    p.add_argument("--paths", nargs="*", help="files or globs this lane expects to touch")
    p.add_argument("--brief", help="file holding the lane's brief")
    p.add_argument("--open", action="store_true", help="also open a terminal with the agent in the lane")
    p = sub.add_parser("open", help="open a new terminal with the agent running in a lane")
    p.add_argument("name")
    p.add_argument("--print", action="store_true", help="print the command instead of opening a terminal")
    sub.add_parser("status", help="list lanes, claims and overlaps")
    p = sub.add_parser("watch", help="control tower: refresh status, run a train when a lane is done")
    p.add_argument("--interval", type=int, default=15, help="seconds between refreshes")
    p.add_argument("--no-train", action="store_true", help="only show status")
    p.add_argument("--once", action="store_true", help="one refresh, then exit")
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
        return {"setup": cmd_setup, "init": cmd_setup, "new": cmd_new, "status": cmd_status, "show": cmd_show,
                "done": cmd_done, "train": cmd_train, "resume": cmd_resume, "open": cmd_open,
                "watch": cmd_watch, "clean": cmd_clean}[a.cmd](y, a)
    except YardError as e:
        print(f"plowline: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
