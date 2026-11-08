"""builtin:mutants: a mutation test of the lines a change touched, and `pl check`.

A test suite can pass while testing nothing: a reversed condition survives when every test happens to use a case
where both branches agree. A mutation engine changes the code on purpose (`<` to `<=`, `not x` to `x`, a call
removed) and runs the tests; a change no test notices is a survivor. Plowline scopes that to the diff: only the
files and lines a train (or a branch, with `pl check`) added or changed are mutated, so the cost follows the size
of the change, not of the repository.

The engines are external tools the user installs. Plowline builds their command, runs it with a time budget,
reads their report and keeps the survivors on changed lines. A missing engine or a run over budget is
UNAVAILABLE, never red: the train stops and blames no lane.
"""
from __future__ import annotations

import ast
import codecs
import difflib
import fnmatch
import json
import os
import re
import shlex
import shutil
import subprocess
import tempfile
import time
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from plowline import gate as gates
from plowline import rules

CONFIG_NAME = "plowline.toml"
DEFAULT_BUDGET = 600  # seconds, for all engines in one run
SHOWN = 20  # survivors listed in a report
TAIL = 15  # lines of an engine's own output shown when it could not run


# --- configuration ----------------------------------------------------------------------------------------------
class ConfigError(Exception):
    pass


@dataclass
class Config:
    max_survivors: int = 0
    budget: float = DEFAULT_BUDGET
    skip: list[str] = field(default_factory=list)
    generated: list[str] = field(default_factory=list)
    engines: dict[str, str] = field(default_factory=dict)  # ".py" -> generic command template


def ext_key(ext: str) -> str:
    ext = ext.strip().lower()
    return ext if ext.startswith(".") else "." + ext


def config_from(data: dict) -> Config:
    """The [mutants] table of plowline.toml (and [plowline].generated), checked."""
    table = data.get("mutants", {})
    if not isinstance(table, dict):
        raise ConfigError("[mutants] must be a table")
    cfg = Config()
    unknown = set(table) - {"max_survivors", "budget", "skip", "engines"}
    if unknown:
        raise ConfigError(f"[mutants]: unknown key(s) {', '.join(sorted(unknown))}")
    ms = table.get("max_survivors", 0)
    if not isinstance(ms, int) or isinstance(ms, bool) or ms < 0:
        raise ConfigError("[mutants] max_survivors must be a whole number, 0 or more")
    budget = table.get("budget", DEFAULT_BUDGET)
    if not isinstance(budget, (int, float)) or isinstance(budget, bool) or budget <= 0:
        raise ConfigError("[mutants] budget must be a number of seconds above 0")
    skip = table.get("skip", [])
    if isinstance(skip, str):
        skip = [skip]
    if not isinstance(skip, list) or not all(isinstance(s, str) for s in skip):
        raise ConfigError("[mutants] skip must be a list of globs, e.g. skip = [\"src/legacy/**\"]")
    engines = {}
    for ext, spec in (table.get("engines") or {}).items():
        command = spec.get("command") if isinstance(spec, dict) else None
        if not isinstance(command, str) or not command.strip():
            raise ConfigError(f"[mutants.engines.{ext}] needs command = \"...\"")
        engines[ext_key(ext)] = command
    generated = data.get("plowline", {}).get("generated", [])
    generated = [generated] if isinstance(generated, str) and generated else \
        [g for g in generated if isinstance(g, str)] if isinstance(generated, list) else []
    cfg.max_survivors, cfg.budget, cfg.skip, cfg.engines, cfg.generated = ms, budget, skip, engines, generated
    return cfg


def load_config(folder: Path) -> Config:
    f = Path(folder) / CONFIG_NAME
    if not f.exists():
        return Config()
    try:
        with f.open("rb") as fh:
            return config_from(tomllib.load(fh))
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"{f}: {e}")


# --- scope: the changed lines -----------------------------------------------------------------------------------
@dataclass
class FileScope:
    path: str  # repository-relative, forward slashes
    ranges: list[tuple[int, int]]  # changed lines on the new side, 1-based, inclusive

    @property
    def lines(self) -> int:
        return sum(b - a + 1 for a, b in self.ranges)

    def touches(self, start: int, end: int | None = None) -> bool:
        end = start if end is None else end
        return any(a <= end and start <= b for a, b in self.ranges)

    def first_touched(self, lines: list[int]) -> int | None:
        return next((n for n in lines if self.touches(n)), None)


@dataclass
class Scope:
    base: str
    head: str
    files: list[FileScope]
    skipped: list[tuple[str, str]] = field(default_factory=list)  # (path, why)

    @property
    def lines(self) -> int:
        return sum(f.lines for f in self.files)


HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")


def _unquote(path: str) -> str:
    """git C-quotes paths with unusual characters: "b/t\\303\\251st.py"."""
    if len(path) > 1 and path[0] == path[-1] == '"':
        raw = codecs.escape_decode(path[1:-1].encode("utf-8"))[0]
        return raw.decode("utf-8", errors="replace")
    return path


def parse_diff(text: str) -> list[FileScope]:
    """Changed files and their added or modified line ranges from `git diff -U0`. Pure deletions (a hunk with no
    new lines, or a deleted file) add nothing; a renamed file is listed under its new name."""
    files: dict[str, list[tuple[int, int]]] = {}
    current: str | None = None
    for line in text.splitlines():
        if line.startswith("diff --git "):
            current = None
        elif line.startswith("+++ "):
            target = _unquote(line[4:].rstrip("\t"))
            current = None if target == "/dev/null" else target.removeprefix("b/")
        elif current and line.startswith("@@"):
            m = HUNK.match(line)
            if not m:
                continue
            start, count = int(m.group(1)), int(m.group(2) if m.group(2) is not None else 1)
            if count:
                files.setdefault(current, []).append((start, start + count - 1))
    out = []
    for path, ranges in files.items():
        merged: list[tuple[int, int]] = []
        for a, b in sorted(ranges):
            if merged and a <= merged[-1][1] + 1:
                merged[-1] = (merged[-1][0], max(b, merged[-1][1]))
            else:
                merged.append((a, b))
        out.append(FileScope(path, merged))
    return out


TEST_NAMES = ("test_*.py", "*_test.*", "*.test.*", "*.spec.*", "*Tests.cs", "*Test.cs")
TEST_DIRS = {"tests", "test", "__tests__"}


def is_test_file(path: str) -> bool:
    parts = path.replace("\\", "/").split("/")
    if any(p in TEST_DIRS or p.endswith((".Tests", ".Test")) for p in parts[:-1]):
        return True
    return any(fnmatch.fnmatchcase(parts[-1], pattern) for pattern in TEST_NAMES)


def filter_scope(changed: list[FileScope], cfg: Config, base: str = "", head: str = "") -> Scope:
    """Drop test files, generated files, `skip` globs and extensions no engine handles."""
    scope = Scope(base, head, [])
    for f in sorted(changed, key=lambda f: f.path):
        if is_test_file(f.path):
            scope.skipped.append((f.path, "test file"))
        elif rules.is_generated(f.path, cfg.generated):
            scope.skipped.append((f.path, "generated"))
        elif rules.in_claim(f.path, cfg.skip):
            scope.skipped.append((f.path, "[mutants] skip"))
        elif engine_for(Path(f.path).suffix, cfg) is None:
            scope.skipped.append((f.path, "no engine for this file type"))
        else:
            scope.files.append(f)
    return scope


def git(tree: Path, *args: str) -> str:
    p = subprocess.run(["git", "-c", "core.quotepath=off", *args], cwd=tree, capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    if p.returncode != 0:
        raise ScopeError(f"git {' '.join(args)} failed: {(p.stderr or p.stdout).strip()}")
    return p.stdout


class ScopeError(Exception):
    pass


def diff_scope(tree: Path, base: str, head: str, cfg: Config) -> Scope:
    text = git(tree, "diff", "-U0", "--no-color", "--no-ext-diff", "-M", base, head, "--")
    return filter_scope(parse_diff(text), cfg, base, head)


# --- running an engine ------------------------------------------------------------------------------------------
class BudgetExceeded(Exception):
    pass


def quote(value: str) -> str:
    """One argument for a shell command line: double quotes for cmd.exe, shlex quoting elsewhere."""
    return f'"{value}"' if os.name == "nt" else shlex.quote(value)


def kill_tree(p: subprocess.Popen) -> None:
    """Stop an engine and everything it started (test runners, compilers, workers)."""
    if os.name == "nt":
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(p.pid)], capture_output=True)
    else:
        import signal
        try:
            os.killpg(p.pid, signal.SIGKILL)
        except OSError:
            pass
    try:
        p.kill()
    except OSError:
        pass


class Runner:
    """Runs engine commands against one shared wall-clock budget."""

    def __init__(self, budget: float, env: dict | None = None):
        self.budget = budget
        self.deadline = time.monotonic() + budget
        self.env = {**os.environ, **(env or {})}

    def run(self, cmd: str, cwd: Path) -> tuple[int, str]:
        left = self.deadline - time.monotonic()
        if left <= 0:
            raise BudgetExceeded()
        group = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt" else {"start_new_session": True}
        p = subprocess.Popen(cmd, shell=True, cwd=cwd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace", env=self.env,
                             **group)
        try:
            out, _ = p.communicate(timeout=left)
        except subprocess.TimeoutExpired:
            kill_tree(p)
            try:
                p.communicate(timeout=10)
            except subprocess.TimeoutExpired:
                pass
            raise BudgetExceeded()
        except BaseException:  # Ctrl-C: the engine runs in its own group, so stop it here
            kill_tree(p)
            raise
        return p.returncode, out or ""


def not_found(code: int, out: str) -> bool:
    return code in gates.NOT_FOUND or (os.name == "nt" and code == 1 and gates.NOT_RECOGNIZED in out)


def tail(text: str, lines: int = TAIL) -> str:
    return "\n".join(text.strip().splitlines()[-lines:])


@dataclass
class Survivor:
    path: str
    line: int
    what: str


@dataclass
class Outcome:
    engine: str
    survivors: list[Survivor] = field(default_factory=list)
    killed: int = 0
    unavailable: str = ""  # why the engine could not give a verdict; "" when it ran
    red: str = ""  # a generic engine said "survivors" (exit 1) without naming any: its output
    note: str = ""


@dataclass
class Job:
    tree: Path  # where the engine runs (the merged train, or your worktree)
    base: str
    head: str
    files: list[FileScope]
    diff_file: Path  # `git diff base head -- <these files>`, for engines that take a diff
    work: Path  # a private folder for reports, removed after the run
    sources: dict[str, str] = field(default_factory=dict)  # path -> text at head

    def scope(self, path: str) -> FileScope | None:
        path = path.replace("\\", "/").removeprefix("./")
        return next((f for f in self.files if f.path == path), None)

    def rel(self, path: str) -> str:
        """An engine's path (absolute, or relative to the tree) as a repository path."""
        p = Path(path)
        if p.is_absolute():
            try:
                return p.resolve().relative_to(self.tree.resolve()).as_posix()
            except ValueError:
                return p.as_posix()
        return path.replace("\\", "/").removeprefix("./")


# --- describing a mutant in words -------------------------------------------------------------------------------
TOKEN = re.compile(r"\w+|\"(?:[^\"\\]|\\.)*\"|'(?:[^'\\]|\\.)*'|===|!==|\*\*=|//=|>>=|<<=|==|!=|<=|>=|\*\*|//|<<|>>"
                   r"|&&|\|\||\+\+|--|\+=|-=|\*=|/=|%=|&=|\|=|\^=|->|=>|::|\?\?|\S")


def _short(text: str, limit: int = 40) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[:limit - 3] + "..."


def describe_change(old: str, new: str) -> str:
    """What a mutant did to a line, in words: `changed < to <=`, `removed not`, `added -`."""
    a = [(m.group(), m.start(), m.end()) for m in TOKEN.finditer(old)]
    b = [(m.group(), m.start(), m.end()) for m in TOKEN.finditer(new)]
    parts = []
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, [t[0] for t in a], [t[0] for t in b],
                                                       autojunk=False).get_opcodes():
        before = old[a[i1][1]:a[i2 - 1][2]] if i2 > i1 else ""
        after = new[b[j1][1]:b[j2 - 1][2]] if j2 > j1 else ""
        if tag == "replace":
            parts.append(f"changed {_short(before)} to {_short(after)}" if after else f"removed {_short(before)}")
        elif tag == "delete":
            parts.append(f"removed {_short(before)}")
        elif tag == "insert":
            parts.append(f"added {_short(after)}")
    return "; ".join(parts[:3]) if parts else "changed the line"


def mutator_words(name: str) -> str:
    """A mutator name as words: `CONDITIONALS_NEGATION` -> `conditionals negation`."""
    name = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", name.replace("_", " "))
    return " ".join(name.split()).lower()


# --- engines ----------------------------------------------------------------------------------------------------
class Engine:
    name = ""
    exts: tuple[str, ...] = ()
    needs = ""  # what to install, for messages and the README table
    leftovers: tuple[str, ...] = ()  # tree paths the engine writes; removed after the run unless they were there

    def available(self, tree: Path) -> str:
        """"" when the engine can run here, else why not."""
        return ""

    def command(self, job: Job) -> str:
        raise NotImplementedError

    def execute(self, job: Job, runner: Runner) -> Outcome:
        why = self.available(job.tree)
        if why:
            return Outcome(self.name, unavailable=why)
        cmd = self.command(job)
        started = time.time()
        code, out = runner.run(cmd, job.tree)
        if not_found(code, out):
            return Outcome(self.name, unavailable=self.missing(code))
        return self.collect(job, code, out, runner, started)

    def collect(self, job: Job, code: int, out: str, runner: Runner, started: float) -> Outcome:
        raise NotImplementedError

    def missing(self, code: int) -> str:
        return f"not found (exit {code}); install {self.needs}"

    def no_report(self, code: int, out: str) -> Outcome:
        return Outcome(self.name, unavailable=f"exited {code} without a report", note=tail(out))


def fresh(path: Path, started: float) -> bool:
    """A report written by this run, not left over from an earlier one."""
    return path.exists() and path.stat().st_mtime >= started - 2


# Python: mutmut 3 ------------------------------------------------------------------------------------------------
MUTMUT_SURVIVED = {0, 5, 33}  # survived, no tests (mutmut 3 status_by_exit_code)
MUTMUT_KILLED = {1, 3, 37, 24, -24, 36, 152, 255}  # killed, caught by type check, timeout (counted as detected)
MUTMUT_KEY = re.compile(r"\.x(?:_(?P<func>\w+?)|ǁ(?P<cls>\w+)ǁ(?P<meth>\w+?))__mutmut_\d+$")


def mutmut_module(path: str) -> str:
    """mutmut's module name for a file: path without .py, dots for slashes, a leading `src.` dropped."""
    module = path[:-3].replace("/", ".") if path.endswith(".py") else path.replace("/", ".")
    return module.removeprefix("src.")


def python_functions(source: str) -> list[tuple[str | None, str, int, int]] | None:
    """(class or None, function, first line, last line) of every top-level function and method: what mutmut
    mutates. None when the file does not parse."""
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return None
    out = []

    def span(node) -> tuple[int, int]:
        return min([node.lineno] + [d.lineno for d in node.decorator_list]), node.end_lineno or node.lineno

    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            out.append((None, node.name, *span(node)))
        elif isinstance(node, ast.ClassDef):
            for child in node.body:
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    out.append((node.name, child.name, *span(child)))
    return out


def parse_mutmut_show(text: str) -> tuple[list[tuple[str, str]], list[str]]:
    """`mutmut show` prints a unified diff of one function: returns the old side as (" " or "-", line) and the
    added lines."""
    old, added, inside = [], [], False
    for line in text.splitlines():
        if line.startswith("@@"):
            inside = True
            continue
        if not inside or line.startswith(("---", "+++")):
            continue
        if line.startswith("-"):
            old.append(("-", line[1:]))
        elif line.startswith("+"):
            added.append(line[1:])
        elif line.startswith(" ") or line == "":
            old.append((" ", line[1:]))
    return old, added


class Mutmut(Engine):
    name = "mutmut"
    exts = (".py",)
    needs = "mutmut 3 (pip install mutmut; it runs on Linux and macOS, or in WSL on Windows)"
    leftovers = ("mutants",)

    def available(self, tree: Path) -> str:
        return "" if shutil.which("mutmut") else "not found on PATH; install mutmut 3 (pip install mutmut)"

    @staticmethod
    def targets(job: Job) -> list[str]:
        """mutmut has no line or diff option, but it takes mutant names with globs: one glob per changed function."""
        globs = []
        for f in job.files:
            module = mutmut_module(f.path)
            functions = python_functions(job.sources.get(f.path, ""))
            if functions is None:
                globs.append(f"{module}.*")
                continue
            for cls, name, start, end in functions:
                if f.touches(start, end):
                    globs.append(f"{module}.x_{name}__mutmut_*" if cls is None else f"{module}.x?{cls}?{name}__mutmut_*")
        return globs

    def command(self, job: Job) -> str:
        globs = self.targets(job)
        return "mutmut run " + " ".join(quote(g) for g in globs) if globs else ""

    def execute(self, job: Job, runner: Runner) -> Outcome:
        if not self.targets(job):
            return Outcome(self.name, note="mutmut: no changed function (mutmut mutates function bodies only)")
        return super().execute(job, runner)

    def collect(self, job, code, out, runner, started) -> Outcome:
        metas = {}
        for f in job.files:
            meta = job.tree / "mutants" / (f.path + ".meta")
            if fresh(meta, started):
                metas[f.path] = meta.read_text(encoding="utf-8")
        if code != 0 or not metas:
            return self.no_report(code, out)
        globs = self.targets(job)
        shows = {}
        for key in self.survivor_keys(metas, globs):
            show_code, text = runner.run(f"mutmut show {quote(key)}", job.tree)
            if show_code == 0:
                shows[key] = text
        survivors, killed = self.parse(metas, shows, job.sources, job.files, globs)
        return Outcome(self.name, survivors, killed)

    @staticmethod
    def statuses(metas: dict[str, str], globs: list[str]) -> dict[str, tuple[str, int | None]]:
        """mutant key -> (file, exit code), for the mutants of the changed functions."""
        out = {}
        for path, text in metas.items():
            for key, code in json.loads(text).get("exit_code_by_key", {}).items():
                if any(fnmatch.fnmatchcase(key, g) for g in globs):
                    out[key] = (path, code)
        return out

    @classmethod
    def survivor_keys(cls, metas: dict[str, str], globs: list[str]) -> list[str]:
        return [k for k, (_, code) in cls.statuses(metas, globs).items() if code in MUTMUT_SURVIVED]

    @classmethod
    def parse(cls, metas: dict[str, str], shows: dict[str, str], sources: dict[str, str], files: list[FileScope],
              globs: list[str]) -> tuple[list[Survivor], int]:
        """Survivors on changed lines, and the mutants killed in the changed functions. Each survivor's line comes
        from `mutmut show`: its diff is placed in the function, found in the source by name."""
        scopes = {f.path: f for f in files}
        statuses = cls.statuses(metas, globs)
        killed = sum(1 for _, code in statuses.values() if code in MUTMUT_KILLED)
        survivors = []
        for key, (path, code) in sorted(statuses.items()):
            if code not in MUTMUT_SURVIVED or path not in scopes:
                continue
            source = sources.get(path, "").splitlines()
            m = MUTMUT_KEY.search(key)
            cls_name, func = (m.group("cls"), m.group("meth") or m.group("func")) if m else (None, "")
            span = next(((s, e) for c, n, s, e in python_functions("\n".join(source)) or []
                         if n == func and c == cls_name), (1, len(source)))
            old, added = parse_mutmut_show(shows.get(key, ""))
            lines = cls._place(source, span, old)
            if lines is None:  # no usable diff: count it on the function if the function changed
                if scopes[path].touches(*span):
                    survivors.append(Survivor(path, span[0], f"mutant {key.rsplit('.', 1)[-1]}"))
                continue
            at = scopes[path].first_touched(lines)
            if at is None:
                continue
            removed = " ".join(text.strip() for kind, text in old if kind == "-")
            survivors.append(Survivor(path, at, describe_change(removed, " ".join(a.strip() for a in added))))
        return survivors, killed

    @staticmethod
    def _place(source: list[str], span: tuple[int, int], old: list[tuple[str, str]]) -> list[int] | None:
        """The file lines a mutant changed: find the diff's old side inside the function (whitespace-insensitive,
        since a method is shown dedented). The search starts a little above the function, because mutmut shows the
        comments above a function as part of it."""
        if not old:
            return None
        want = [t.strip() for _, t in old]
        start, end = span
        for i in range(max(0, start - 1 - len(want)), min(end, len(source)) - len(want) + 1):
            if all(source[i + k].strip() == want[k] for k in range(len(want))):
                removed = [i + 1 + k for k, (kind, _) in enumerate(old) if kind == "-"]
                return removed or [i + 1]
        return None


# JavaScript and TypeScript: StrykerJS ----------------------------------------------------------------------------
STRYKER_SURVIVED = {"Survived", "NoCoverage"}
STRYKER_KILLED = {"Killed", "Timeout"}


def parse_stryker_json(text: str, job: Job) -> tuple[list[Survivor], int]:
    """A mutation-testing-elements report (StrykerJS and Stryker.NET): survivors on changed lines, kills there."""
    report = json.loads(text)
    survivors, killed = [], 0
    for key, entry in (report.get("files") or {}).items():
        scope = job.scope(job.rel(key))
        if scope is None:
            continue
        source = (entry.get("source") or "").splitlines()
        for mutant in entry.get("mutants") or []:
            loc = mutant.get("location") or {}
            start, end = loc.get("start") or {}, loc.get("end") or {}
            first, last = int(start.get("line", 0)), int(end.get("line", start.get("line", 0)))
            if not scope.touches(first, last):
                continue
            status = mutant.get("status")
            if status in STRYKER_KILLED:
                killed += 1
            elif status in STRYKER_SURVIVED:
                line = scope.first_touched(list(range(first, last + 1))) or first
                survivors.append(Survivor(scope.path, line, _stryker_words(mutant, source, start, end)))
    return survivors, killed


def _stryker_words(mutant: dict, source: list[str], start: dict, end: dict) -> str:
    replacement = mutant.get("replacement")
    if replacement is not None and start.get("line") == end.get("line") and 0 < start["line"] <= len(source):
        text = source[start["line"] - 1]
        original = text[max(0, start.get("column", 1) - 1):max(0, end.get("column", 1) - 1)]
        if original and "\n" not in replacement:
            return describe_change(original, replacement) if original != replacement else "changed the line"
    words = mutator_words(mutant.get("mutatorName", "mutant"))
    return f"{words}: {_short(replacement)}" if replacement else words


class StrykerJS(Engine):
    name = "StrykerJS"
    exts = (".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs")
    needs = "StrykerJS (npm install --save-dev @stryker-mutator/core and a test-runner plugin)"
    leftovers = ("reports/mutation/mutation.json", ".stryker-tmp")
    report = "reports/mutation/mutation.json"

    def available(self, tree: Path) -> str:
        local = any((tree / "node_modules" / ".bin" / n).exists() for n in ("stryker", "stryker.cmd"))
        if (local or shutil.which("stryker")) and shutil.which("npx"):
            return ""
        return "not found (no node_modules/.bin/stryker); npm install --save-dev @stryker-mutator/core"

    def command(self, job: Job) -> str:
        ranges = ",".join(f"{f.path}:{a}-{b}" for f in job.files for a, b in f.ranges)
        return f"npx --no stryker run --mutate {quote(ranges)} --reporters json"

    def collect(self, job, code, out, runner, started) -> Outcome:
        report = job.tree / self.report
        if not fresh(report, started):
            return self.no_report(code, out)
        survivors, killed = parse_stryker_json(report.read_text(encoding="utf-8"), job)
        return Outcome(self.name, survivors, killed)


# C#: Stryker.NET -------------------------------------------------------------------------------------------------
class StrykerNet(Engine):
    name = "Stryker.NET"
    exts = (".cs",)
    needs = "Stryker.NET (dotnet tool install dotnet-stryker)"

    def available(self, tree: Path) -> str:
        if not shutil.which("dotnet"):
            return "dotnet not found on PATH; Stryker.NET needs the .NET SDK"
        manifest = tree / ".config" / "dotnet-tools.json"
        local = manifest.exists() and "dotnet-stryker" in manifest.read_text(encoding="utf-8", errors="replace")
        if local or shutil.which("dotnet-stryker"):
            return ""
        return "not found; dotnet tool install dotnet-stryker"

    def out_dir(self, job: Job) -> Path:
        return job.work / "stryker-net"

    def command(self, job: Job) -> str:
        # Stryker.NET's mutate spans are character offsets, not lines: --since scopes it to the changed files,
        # and Plowline keeps the survivors on changed lines.
        return f"dotnet stryker --since:{job.base} -r json -O {quote(str(self.out_dir(job)))}"

    def execute(self, job: Job, runner: Runner) -> Outcome:
        self.out_dir(job).mkdir(parents=True, exist_ok=True)  # Stryker.NET wants the output folder to exist
        return super().execute(job, runner)

    def collect(self, job, code, out, runner, started) -> Outcome:
        report = self.out_dir(job) / "reports" / "mutation-report.json"
        if not report.exists():
            return self.no_report(code, out)
        survivors, killed = parse_stryker_json(report.read_text(encoding="utf-8"), job)
        return Outcome(self.name, survivors, killed)


# Rust: cargo-mutants ---------------------------------------------------------------------------------------------
CARGO_LINE = re.compile(r"^(?P<path>[^:]+):(?P<line>\d+):(?P<col>\d+):\s*(?P<what>.*)$")


def parse_cargo_mutants(missed: str, caught: str, timeout: str, job: Job) -> tuple[list[Survivor], int]:
    """cargo-mutants' missed.txt / caught.txt / timeout.txt: `src/lib.rs:5:9: replace is_even -> bool with true`."""
    def entries(text: str):
        for line in text.splitlines():
            m = CARGO_LINE.match(line.strip())
            if m:
                scope = job.scope(m.group("path"))
                if scope and scope.touches(int(m.group("line"))):
                    yield scope.path, int(m.group("line")), m.group("what").strip()

    survivors = [Survivor(p, n, w) for p, n, w in entries(missed)]
    return survivors, sum(1 for _ in entries(caught)) + sum(1 for _ in entries(timeout))


class CargoMutants(Engine):
    name = "cargo-mutants"
    exts = (".rs",)
    needs = "cargo-mutants (cargo install --locked cargo-mutants)"

    def available(self, tree: Path) -> str:
        if shutil.which("cargo") and shutil.which("cargo-mutants"):
            return ""
        return "not found on PATH; cargo install --locked cargo-mutants"

    def command(self, job: Job) -> str:
        return f"cargo mutants --in-diff {quote(str(job.diff_file))} --output {quote(str(job.work))}"

    def collect(self, job, code, out, runner, started) -> Outcome:
        folder = job.work / "mutants.out"
        if code == 4:
            return Outcome(self.name, unavailable="the tests fail before any mutation (exit 4)",
                           note=tail(out))
        if code not in (0, 2, 3) or not folder.exists():
            return self.no_report(code, out)

        def read(name: str) -> str:
            f = folder / name
            return f.read_text(encoding="utf-8", errors="replace") if f.exists() else ""
        survivors, killed = parse_cargo_mutants(read("missed.txt"), read("caught.txt"), read("timeout.txt"), job)
        return Outcome(self.name, survivors, killed)


# Go: Gremlins ----------------------------------------------------------------------------------------------------
GREMLINS_SURVIVED = {"LIVED", "NOT COVERED"}
GREMLINS_KILLED = {"KILLED", "TIMED OUT"}
GREMLINS_WORDS = {
    "CONDITIONALS_BOUNDARY": "changed a comparison boundary (< to <=)",
    "CONDITIONALS_NEGATION": "negated a comparison (== to !=)",
    "ARITHMETIC_BASE": "changed an arithmetic operator (+ to -)",
    "INCREMENT_DECREMENT": "swapped ++ and --",
    "INVERT_NEGATIVES": "removed a unary minus",
    "INVERT_LOGICAL": "swapped && and ||",
    "INVERT_LOOPCTRL": "swapped break and continue",
    "INVERT_ASSIGNMENTS": "inverted an assignment operator (+= to -=)",
    "INVERT_BITWISE": "inverted a bitwise operator (& to |)",
    "INVERT_BWASSIGN": "inverted a bitwise assignment (&= to |=)",
    "REMOVE_SELF_ASSIGNMENTS": "turned x op= y into x = y",
}


def parse_gremlins(text: str, job: Job) -> tuple[list[Survivor], int]:
    """Gremlins' -o JSON: files[].mutations[] with line, type and status."""
    report = json.loads(text)
    survivors, killed = [], 0
    for entry in report.get("files") or []:
        name = job.rel(entry.get("file_name", ""))
        scope = job.scope(name) or next((f for f in job.files if f.path.endswith("/" + name)), None)
        if scope is None:
            continue
        for mutation in entry.get("mutations") or []:
            line = int(mutation.get("line", 0))
            if not scope.touches(line):
                continue
            status = mutation.get("status")
            if status in GREMLINS_KILLED:
                killed += 1
            elif status in GREMLINS_SURVIVED:
                kind = mutation.get("type", "")
                survivors.append(Survivor(scope.path, line, GREMLINS_WORDS.get(kind, mutator_words(kind))))
    return survivors, killed


class Gremlins(Engine):
    name = "Gremlins"
    exts = (".go",)
    needs = "Gremlins (go install github.com/go-gremlins/gremlins/cmd/gremlins@latest)"

    def available(self, tree: Path) -> str:
        return "" if shutil.which("gremlins") else \
            "not found on PATH; go install github.com/go-gremlins/gremlins/cmd/gremlins@latest"

    def command(self, job: Job) -> str:
        return f"gremlins unleash --diff {job.base} -o {quote(str(job.work / 'gremlins.json'))}"

    def collect(self, job, code, out, runner, started) -> Outcome:
        report = job.work / "gremlins.json"
        if not report.exists():
            return self.no_report(code, out)
        survivors, killed = parse_gremlins(report.read_text(encoding="utf-8"), job)
        return Outcome(self.name, survivors, killed)


# Any language: a configured command ------------------------------------------------------------------------------
GENERIC_LINE = re.compile(r"^\s*(?P<path>[^\s:][^:]*):(?P<line>\d+)(?::\d+)?[:\s]\s*(?P<what>.*)$")


class Generic(Engine):
    """[mutants.engines.<ext>] command = "...": exit 0 = no survivors, exit 1 = survivors (its output is the report;
    lines like `path:line what` are listed), anything else = unavailable."""
    needs = "the command in [mutants.engines]"

    def __init__(self, ext: str, template: str):
        self.ext, self.template = ext, template
        self.exts = (ext,)
        self.name = f"[mutants.engines.{ext.lstrip('.')}]"

    def missing(self, code: int) -> str:
        return f"its command was not found (exit {code})"

    def command(self, job: Job) -> str:
        values = {
            "{files}": " ".join(quote(f.path) for f in job.files),
            "{ranges}": " ".join(quote(f"{f.path}:{a}-{b}") for f in job.files for a, b in f.ranges),
            "{base}": job.base, "{head}": job.head, "{diff}": quote(str(job.diff_file)),
        }
        cmd = self.template
        for key, value in values.items():
            cmd = cmd.replace(key, value)
        return cmd

    def collect(self, job, code, out, runner, started) -> Outcome:
        if code == 0:
            return Outcome(self.name)
        if code != 1:
            return Outcome(self.name, unavailable=f"exited {code} (0 = no survivors, 1 = survivors)",
                           note=tail(out))
        survivors = parse_generic(out, job)
        return Outcome(self.name, survivors, red="" if survivors else tail(out, 40) or "(no output)")


def parse_generic(out: str, job: Job) -> list[Survivor]:
    survivors = []
    for line in out.splitlines():
        m = GENERIC_LINE.match(line)
        if m:
            scope = job.scope(m.group("path"))
            if scope and scope.touches(int(m.group("line"))):
                survivors.append(Survivor(scope.path, int(m.group("line")), m.group("what").strip() or "survived"))
    return survivors


BUILTIN_ENGINES: list[Engine] = [Mutmut(), StrykerJS(), StrykerNet(), CargoMutants(), Gremlins()]


def engine_for(ext: str, cfg: Config) -> Engine | None:
    ext = ext.lower()
    if ext in cfg.engines:
        return Generic(ext, cfg.engines[ext])
    return next((e for e in BUILTIN_ENGINES if ext in e.exts), None)


def group_files(scope: Scope, cfg: Config) -> list[tuple[Engine, list[FileScope]]]:
    groups: dict[str, tuple[Engine, list[FileScope]]] = {}
    for f in scope.files:
        engine = engine_for(Path(f.path).suffix, cfg)
        groups.setdefault(engine.name, (engine, []))[1].append(f)
    return list(groups.values())


# --- the driver -------------------------------------------------------------------------------------------------
@dataclass
class Result:
    status: str  # pass | fail | unavailable
    line: str  # the completion line
    report: str  # the line, then survivors and notes
    survivors: list[Survivor] = field(default_factory=list)
    killed: int = 0
    changed_lines: int = 0


def budget_message(budget: float) -> str:
    return f"mutants: budget of {budget:g} s exceeded; raise [mutants] budget or narrow the scope with skip"


def _plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def survivor_lines(survivors: list[Survivor]) -> list[str]:
    lines = [f"{s.path}:{s.line}  {s.what}  (no test failed)" for s in survivors[:SHOWN]]
    if len(survivors) > SHOWN:
        lines.append(f"... and {len(survivors) - SHOWN} more")
    return lines


def unavailable(reason: str, note: str = "") -> Result:
    line = f"MUTANTS: UNAVAILABLE ({reason})"
    return Result("unavailable", line, line + (f"\n{note}" if note else ""))


def prepare(tree: Path, base: str, head: str, cfg: Config | None) -> tuple[Config, Scope]:
    cfg = cfg if cfg is not None else load_config(tree)
    return cfg, diff_scope(tree, base, head, cfg)


def write_job(tree: Path, scope: Scope, files: list[FileScope], work: Path, n: int) -> Job:
    diff_file = work / f"scope-{n}.diff"
    diff_file.write_text(git(tree, "diff", "--no-color", "--no-ext-diff", "-M", scope.base, scope.head, "--",
                             *[f.path for f in files]), encoding="utf-8")
    sources = {}
    for f in files:
        try:
            sources[f.path] = git(tree, "show", f"{scope.head}:{f.path}")
        except ScopeError:
            sources[f.path] = ""
    return Job(tree, scope.base, scope.head, files, diff_file, work, sources)


def _snapshot(tree: Path, paths: tuple[str, ...]) -> set[Path]:
    """The leftover paths, and their parent folders, that exist before an engine runs."""
    seen = set()
    for rel in paths:
        p = tree / rel
        while p != tree and p != p.parent:
            if p.exists():
                seen.add(p)
            p = p.parent
    return seen


def _clean(tree: Path, paths: tuple[str, ...], before: set[Path]) -> None:
    """Remove what an engine wrote into the tree, so `pl done` still finds a clean worktree."""
    for rel in paths:
        p = tree / rel
        if p in before or not p.exists():
            continue
        shutil.rmtree(p, ignore_errors=True) if p.is_dir() else p.unlink(missing_ok=True)
        parent = p.parent
        while parent != tree and parent not in before and parent.exists() and not any(parent.iterdir()):
            parent.rmdir()
            parent = parent.parent


def run(tree: Path, base: str, head: str, cfg: Config | None = None, env: dict | None = None) -> Result:
    """Mutate the lines base..head added or changed, in `tree`, and judge the survivors."""
    tree = Path(tree)
    try:
        cfg, scope = prepare(tree, base, head, cfg)
    except (ConfigError, ScopeError) as e:
        return unavailable(str(e))
    if not scope.files:
        return Result("pass", "MUTANTS: nothing to mutate", "MUTANTS: nothing to mutate")
    runner = Runner(cfg.budget, {"PLOWLINE_BASE": base, "PLOWLINE_HEAD": head, **(env or {})})
    outcomes: list[Outcome] = []
    over_budget = False
    work = Path(tempfile.mkdtemp(prefix="plowline-mutants-"))
    try:
        for n, (engine, files) in enumerate(group_files(scope, cfg)):
            job = write_job(tree, scope, files, work, n)
            before = _snapshot(tree, engine.leftovers)
            try:
                outcomes.append(engine.execute(job, runner))
            except BudgetExceeded:
                over_budget = True
                break
            finally:
                _clean(tree, engine.leftovers, before)
    except ScopeError as e:
        return unavailable(str(e))
    finally:
        shutil.rmtree(work, ignore_errors=True)
    return verdict(scope, cfg, outcomes, over_budget)


def verdict(scope: Scope, cfg: Config, outcomes: list[Outcome], over_budget: bool = False) -> Result:
    """FAIL when more survivors than max_survivors are proven (or a generic engine said so); otherwise any engine
    that could not run, or the budget running out, makes it UNAVAILABLE; otherwise PASS."""
    survivors = sorted((s for o in outcomes for s in o.survivors), key=lambda s: (s.path, s.line))
    killed = sum(o.killed for o in outcomes)
    on = f"on {_plural(scope.lines, 'changed line')}"
    lost = [o for o in outcomes if o.unavailable]
    reasons = [f"{o.engine}: {o.unavailable}" for o in lost] + ([budget_message(cfg.budget)] if over_budget else [])
    notes = [o.note for o in outcomes if o.note]
    red = [o for o in outcomes if o.red]
    if len(survivors) > cfg.max_survivors or red:
        count = len(survivors) if survivors else "unlisted"
        line = f"MUTANTS: FAIL ({count} survivor{'' if len(survivors) == 1 else 's'} {on})"
        body = survivor_lines(survivors) + [f"{o.engine} reported survivors (exit 1):\n{o.red}" for o in red]
        status, notes = "fail", reasons + notes
    elif reasons:
        line = f"MUTANTS: UNAVAILABLE ({'; '.join(reasons)})"
        body, status = [], "unavailable"
    else:
        allowed = (f"; {_plural(len(survivors), 'survivor')} allowed by max_survivors = {cfg.max_survivors}"
                   if survivors else "")
        line = f"MUTANTS: PASS ({_plural(killed, 'mutant')} killed {on}{allowed})"
        body, status = survivor_lines(survivors), "pass"
    return Result(status, line, "\n".join([line, *body, *notes]), survivors, killed, scope.lines)


def explain(tree: Path, base: str, head: str, cfg: Config | None = None) -> str:
    """The scope and the exact commands `pl check` would run. Runs no engine."""
    tree = Path(tree)
    cfg, scope = prepare(tree, base, head, cfg)
    out = [f"scope: {base[:12]}..{head[:12]}, {_plural(len(scope.files), 'file')}, "
           f"{_plural(scope.lines, 'changed line')}"]
    for f in scope.files:
        out.append(f"  {f.path}  lines {', '.join(f'{a}-{b}' if a != b else str(a) for a, b in f.ranges)}")
    for path, why in scope.skipped:
        out.append(f"  skipped {path} ({why})")
    if not scope.files:
        out.append("MUTANTS: nothing to mutate")
        return "\n".join(out)
    out.append(f"budget: {cfg.budget:g} s for all engines; max_survivors = {cfg.max_survivors}")
    work = Path(tempfile.mkdtemp(prefix="plowline-mutants-"))
    try:
        for n, (engine, files) in enumerate(group_files(scope, cfg)):
            job = write_job(tree, scope, files, work, n)
            why = engine.available(tree)
            out.append(f"{engine.name}: {', '.join(f.path for f in files)}")
            cmd = engine.command(job)
            out.append(f"  run: {cmd}" if cmd else "  run: nothing (no changed function: mutmut mutates function bodies)")
            if why:
                out.append(f"  unavailable here: {why}")
    finally:
        shutil.rmtree(work, ignore_errors=True)
    return "\n".join(out)


def gate_step(ctx: "gates.GateContext") -> "gates.StepResult":
    """builtin:mutants in a gate list: scope is ctx.base..ctx.head in ctx.tree."""
    result = run(ctx.tree, ctx.base, ctx.head,
                 env={"PLOWLINE_LANES": " ".join(ctx.lanes)})
    return gates.StepResult(result.status, result.report, result.line)
