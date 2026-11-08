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
import sys
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
    header = False  # between `diff --git` and the file's first hunk: the only place `+++ ` names a file
    for line in text.split("\n"):
        if line.startswith("diff --git "):
            current, header = None, True
        elif header and line.startswith("+++ "):
            target = _unquote(line[4:].rstrip("\t\r"))
            current = None if target == "/dev/null" else target.removeprefix("b/")
        elif line.startswith("@@"):
            header = False
            if not current:
                continue
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


TEST_NAMES = ("test_*.py", "*_test.*", "tests.py", "*_tests.py", "conftest.py", "*.test.*", "*.spec.*", "*.cy.*",
              "*Tests.cs", "*Test.cs", "*Spec.cs")
# Any folder with one of these names holds tests. A production package named `test` or `tests` is skipped too
# (the README says so under Limits).
TEST_DIRS = {"tests", "test", "__tests__", "e2e"}


def is_test_file(path: str) -> bool:
    """Test code is never mutated: its mutants would only test the tests."""
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


WORKTREE = ""  # as `head`: the working tree itself (pl check --dirty), not a commit


def diff_args(base: str, head: str) -> list[str]:
    return [base, head] if head != WORKTREE else [base]


def diff_scope(tree: Path, base: str, head: str, cfg: Config) -> Scope:
    text = git(tree, "diff", "-U0", "--no-color", "--no-ext-diff", "-M", *diff_args(base, head), "--")
    return filter_scope(parse_diff(text), cfg, base, head)


# --- running an engine ------------------------------------------------------------------------------------------
class BudgetExceeded(Exception):
    pass


class QuoteError(ValueError):
    pass


def quote(value: str) -> str:
    """One argument for a shell command line: shlex quoting, or double quotes for cmd.exe. cmd.exe has no escape for
    a double quote inside quotes, so a value holding one is refused (no path or commit id needs it). It expands
    %NAME% even inside quotes, so each % is written outside them, escaped: "a"^%"b". A run of backslashes just
    before a closing quote is doubled, or the program would read the quote as escaped."""
    if os.name != "nt":
        return shlex.quote(value)
    if '"' in value:
        raise QuoteError(f"refusing to quote {value!r} for cmd.exe: it contains a double quote")
    value = re.sub(r"(\\+)(?=%|$)", lambda m: m.group(1) * 2, value)
    return '"' + value.replace("%", '"^%"') + '"'


def kill_tree(p: subprocess.Popen) -> str:
    """Stop an engine and everything it started (test runners, compilers, workers). Returns "" or what failed."""
    problem = ""
    if os.name == "nt":
        k = subprocess.run(["taskkill", "/F", "/T", "/PID", str(p.pid)], capture_output=True, text=True,
                           errors="replace")
        if k.returncode != 0 and p.poll() is None:
            problem = f"taskkill /T exited {k.returncode}: {(k.stderr or k.stdout).strip()}"
    else:
        import signal
        try:
            os.killpg(p.pid, signal.SIGKILL)
        except OSError as e:
            if p.poll() is None:
                problem = f"killpg failed: {e}"
    try:
        p.kill()
    except OSError:
        pass
    return problem


class Runner:
    """Runs engine commands against one shared wall-clock budget."""

    def __init__(self, budget: float, env: dict | None = None):
        self.budget = budget
        self.deadline = time.monotonic() + budget
        self.env = {**os.environ, **(env or {})}
        self.warnings: list[str] = []

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
            problem = kill_tree(p)
            try:
                p.communicate(timeout=10)
            except subprocess.TimeoutExpired:
                problem = problem or "a process it started still holds its output open"
            if problem:
                self.warnings.append(f"the engine may still be running ({problem}); stop it by hand: {cmd}")
            raise BudgetExceeded()
        except BaseException:  # Ctrl-C: the engine runs in its own group, so stop it here
            problem = kill_tree(p)
            if problem:
                print(f"plowline: the engine may still be running ({problem}); stop it by hand: {cmd}", file=sys.stderr)
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
class Parsed:
    """What a parser read from an engine's report, already limited to the changed lines."""
    survivors: list[Survivor] = field(default_factory=list)
    killed: int = 0
    # repository path -> mutants judged on its changed lines, for every scoped file the report covers. A scoped file
    # missing here was not covered at all (a path or naming mismatch), and that is never a pass.
    evaluated: dict[str, int] = field(default_factory=dict)
    unjudged: list[str] = field(default_factory=list)  # mutants on changed lines with a status we cannot judge

    def count(self, path: str, n: int = 1) -> None:
        self.evaluated[path] = self.evaluated.get(path, 0) + n


@dataclass
class Outcome:
    engine: str
    survivors: list[Survivor] = field(default_factory=list)
    killed: int = 0
    unavailable: str = ""  # why the engine could not give a verdict; "" when it ran
    note: str = ""
    evaluated: dict[str, int] | None = None  # see Parsed; None when the engine cannot say (a generic command)

    @classmethod
    def of(cls, engine: str, parsed: Parsed) -> "Outcome":
        if parsed.unjudged:
            more = f" and {len(parsed.unjudged) - 3} more" if len(parsed.unjudged) > 3 else ""
            return cls(engine, unavailable=f"it could not judge {len(parsed.unjudged)} mutant(s) on changed lines "
                                           f"({', '.join(parsed.unjudged[:3])}{more})")
        return cls(engine, parsed.survivors, parsed.killed, evaluated=parsed.evaluated)


IMPORT_LINES = {
    ".py": re.compile(r"^(import\s|from\s+\S+\s+import\b)"),
    ".rs": re.compile(r"^(pub(\([^)]*\))?\s+)?use\s"),
    ".go": re.compile(r"^(import\b|(\w+\s+|[._]\s+)?\"[^\"]*\"$)"),
    ".cs": re.compile(r"^(global\s+)?using\s+[^(=]*;$|^using\s+\w+\s*=\s*[\w.]+;$"),
}
JS_IMPORT = re.compile(r"^(import\s|export\s+(\*|\{[^}]*\}|type\s+\{[^}]*\})\s*from\s)")
PUNCTUATION_ONLY = re.compile(r"^[{}()\[\];,]*$")


def without_c_comments(lines: list[str]) -> list[str]:
    """Each line of a C-family file with its `//` and `/* ... */` comments removed, following a block comment
    across lines. A `*` is a comment only inside an open block, so `*p = v;` stays code. Double-quoted and
    backtick strings are skipped, so a `/*` inside one opens nothing."""
    out, in_block = [], False
    for line in lines:
        kept, i = [], 0
        while i < len(line):
            if in_block:
                end = line.find("*/", i)
                in_block, i = (True, len(line)) if end < 0 else (False, end + 2)
            elif line[i] in "\"`":
                j = i + 1
                while j < len(line) and line[j] != line[i]:
                    j += 2 if line[j] == "\\" else 1
                kept.append(line[i:j + 1])
                i = j + 1
            elif line.startswith("//", i):
                break
            elif line.startswith("/*", i):
                in_block, i = True, i + 2
            else:
                kept.append(line[i])
                i += 1
        out.append("".join(kept))
    return out


def docstring_lines(source: str) -> set[int]:
    """The lines of every docstring (a string that is the first statement of a module, class or function)."""
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return set()
    out: set[int] = set()
    for node in ast.walk(tree):
        first = getattr(node, "body", None)
        first = first[0] if isinstance(first, list) and first else None
        if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant) and isinstance(first.value.value, str):
            out.update(range(first.lineno, (first.end_lineno or first.lineno) + 1))
    return out


def code_lines(path: str, source: str, ranges: list[tuple[int, int]]) -> list[int]:
    """The changed lines that hold code an engine could mutate: not blank, not a comment or a docstring, not only an
    import or brackets. For Python only lines inside a function count (mutmut mutates function bodies only); a
    Python file that does not parse has every such line counted. A line the source does not have counts as code,
    to stay on the safe side."""
    ext = Path(path).suffix.lower()
    lines = lines_of(source)
    imports = IMPORT_LINES.get(ext, JS_IMPORT)
    if ext == ".py":
        functions = python_functions(source)
        spans = None if functions is None else [(s, e) for _, _, s, e in functions]
        skip = docstring_lines(source)
        texts = ["" if line.strip().startswith("#") else line for line in lines]
    else:
        spans, skip, texts = None, set(), without_c_comments(lines)
    out = []
    for a, b in ranges:
        for n in range(a, b + 1):
            if n > len(lines):
                out.append(n)
                continue
            text = texts[n - 1].strip()
            if not text or n in skip or PUNCTUATION_ONLY.match(text) or imports.match(text):
                continue
            if spans is not None and not any(s <= n <= e for s, e in spans):
                continue
            out.append(n)
    return out


def check_coverage(outcome: Outcome, files: list[FileScope], sources: dict[str, str] | None = None) -> Outcome:
    """A scoped file whose changed lines hold code but got no judged mutant was not tested: UNAVAILABLE, not PASS,
    whether the engine's report left the file out (a path or module name mismatch) or skipped, ignored or could not
    compile its mutants. An edit only to comments, blank lines, imports or brackets passes, named in a note."""
    if outcome.unavailable or outcome.evaluated is None:
        return outcome
    sources = sources or {}
    problems, quiet = [], []
    for f in files:
        judged = outcome.evaluated.get(f.path)
        if judged:
            continue
        code = code_lines(f.path, sources.get(f.path, ""), f.ranges)
        if not code:
            quiet.append(f.path)
        elif judged is None:
            problems.append(f"its report has nothing for {f.path}")
        else:
            shown = ", ".join(map(str, code[:5])) + (" ..." if len(code) > 5 else "")
            problems.append(f"no mutant was judged on the changed code of {f.path} (line {shown})")
    if problems:
        return Outcome(outcome.engine, unavailable="; ".join(problems), note=outcome.note)
    if quiet:
        what = f"{outcome.engine}: only comments, docstrings, blank lines or imports changed in {', '.join(quiet)}"
        outcome.note = "\n".join(filter(None, [outcome.note, what]))
    return outcome


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
    """mutmut's module name for a file: path without .py, dots for slashes, a leading `src.` dropped, and a
    package's `__init__` left out (mutmut names `pkg/__init__.py` mutants `pkg.x_f__mutmut_1`)."""
    module = path[:-3].replace("/", ".") if path.endswith(".py") else path.replace("/", ".")
    module = module.removeprefix("src.")
    return module.removesuffix(".__init__") if module.endswith(".__init__") else module


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


def lines_of(text: str) -> list[str]:
    """A file's lines as git counts them: split on newline only (a form feed or U+2028 is not a line end)."""
    lines = text.replace("\r\n", "\n").split("\n")
    return lines[:-1] if lines and lines[-1] == "" else lines


def parse_mutmut_show(text: str) -> tuple[list[tuple[str, str]], list[str]]:
    """`mutmut show` prints a unified diff of one function: returns the old side as (" " or "-", line) and the
    added lines."""
    old, added, inside = [], [], False
    for line in lines_of(text):
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
    def file_targets(path: str, ranges: FileScope, source: str) -> list[str]:
        module = mutmut_module(path)
        functions = python_functions(source)
        if functions is None:
            return [f"{module}.*"]
        return [f"{module}.x_{name}__mutmut_*" if cls is None else f"{module}.x?{cls}?{name}__mutmut_*"
                for cls, name, start, end in functions if ranges.touches(start, end)]

    @classmethod
    def targets(cls, job: Job) -> list[str]:
        """mutmut has no line or diff option, but it takes mutant names with globs: one glob per changed function."""
        return [g for f in job.files for g in cls.file_targets(f.path, f, job.sources.get(f.path, ""))]

    def command(self, job: Job) -> str:
        globs = self.targets(job)
        return "mutmut run " + " ".join(quote(g) for g in globs) if globs else ""

    def execute(self, job: Job, runner: Runner) -> Outcome:
        if not self.targets(job):
            return Outcome(self.name, evaluated={f.path: 0 for f in job.files},
                           note="mutmut: no changed function (mutmut mutates function bodies only)")
        return super().execute(job, runner)

    def collect(self, job, code, out, runner, started) -> Outcome:
        if code != 0:
            return self.no_report(code, out)
        metas, stale = {}, []
        for f in job.files:
            meta = job.tree / "mutants" / (f.path + ".meta")
            if fresh(meta, started):
                metas[f.path] = meta.read_text(encoding="utf-8")
            elif self.file_targets(f.path, f, job.sources.get(f.path, "")):
                stale.append(f.path)
        if stale:
            return Outcome(self.name, unavailable=f"no fresh results for {', '.join(stale)}", note=tail(out))
        globs = self.targets(job)
        shows = {}
        for key in self.survivor_keys(metas, globs):
            show_code, text = runner.run(f"mutmut show {quote(key)}", job.tree)
            if show_code == 0:
                shows[key] = text
        return Outcome.of(self.name, self.parse(metas, shows, job.sources, job.files, globs))

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
              globs: list[str]) -> Parsed:
        """Survivors on changed lines, and the mutants killed in the changed functions. Each survivor's line comes
        from `mutmut show`: its diff is placed in the function, found in the source by name. A file counts as
        covered when its results hold mutants of the changed functions (or it has no changed function)."""
        parsed = Parsed()
        for f in files:
            if not cls.file_targets(f.path, f, sources.get(f.path, "")):
                parsed.count(f.path, 0)
        for key, (path, code) in sorted(cls.statuses(metas, globs).items()):
            scope = next((f for f in files if f.path == path), None)
            if scope is None:
                continue
            parsed.count(path, 0)
            if code in MUTMUT_KILLED:
                parsed.killed += 1
                parsed.count(path)
                continue
            if code not in MUTMUT_SURVIVED:
                parsed.unjudged.append(f"{key.rsplit('.', 1)[-1]}: exit {code}")
                continue
            source = lines_of(sources.get(path, ""))
            m = MUTMUT_KEY.search(key)
            cls_name, func = (m.group("cls"), m.group("meth") or m.group("func")) if m else (None, "")
            span = next(((s, e) for c, n, s, e in python_functions("\n".join(source)) or []
                         if n == func and c == cls_name), (1, len(source)))
            old, added = parse_mutmut_show(shows.get(key, ""))
            lines = cls._place(source, span, old)
            if lines is None:  # no usable diff, or an ambiguous one: count it on the function if the function changed
                if scope.touches(*span):
                    parsed.survivors.append(Survivor(path, span[0], f"mutant {key.rsplit('.', 1)[-1]}"))
                    parsed.count(path)
                continue
            at = scope.first_touched(lines)
            if at is None:
                continue
            parsed.count(path)
            removed = " ".join(text.strip() for kind, text in old if kind == "-")
            parsed.survivors.append(Survivor(path, at, describe_change(removed, " ".join(a.strip() for a in added))))
        return parsed

    @staticmethod
    def _place(source: list[str], span: tuple[int, int], old: list[tuple[str, str]]) -> list[int] | None:
        """The file lines a mutant changed: find the diff's old side in the file (whitespace-insensitive, since a
        method is shown dedented). The search starts a little above the function, because mutmut shows the
        comments above a function as part of it. One match inside the function wins; with several, or none, the
        caller falls back to the function's span."""
        if not old:
            return None
        want = [t.strip() for _, t in old]
        start, end = span
        matches = [i for i in range(max(0, start - 1 - len(want)), min(end, len(source)) - len(want) + 1)
                   if all(source[i + k].strip() == want[k] for k in range(len(want)))]
        inside = [i for i in matches if i >= start - 1] or matches
        if len(inside) != 1:
            return None
        i = inside[0]
        return [i + 1 + k for k, (kind, _) in enumerate(old) if kind == "-"] or [i + 1]


# JavaScript and TypeScript: StrykerJS ----------------------------------------------------------------------------
STRYKER_SURVIVED = {"Survived", "NoCoverage"}
STRYKER_KILLED = {"Killed", "Timeout"}
STRYKER_NOT_RUN = {"CompileError", "RuntimeError", "Ignored"}  # not viable, or excluded: neither kills nor survivors


def parse_stryker_json(text: str, job: Job) -> Parsed:
    """A mutation-testing-elements report (StrykerJS and Stryker.NET). A mutant counts when its span overlaps a
    changed line."""
    report = json.loads(text)
    parsed = Parsed()
    for key, entry in (report.get("files") or {}).items():
        scope = job.scope(job.rel(key))
        if scope is None:
            continue
        parsed.count(scope.path, 0)
        source = lines_of(entry.get("source") or "")
        for mutant in entry.get("mutants") or []:
            loc = mutant.get("location") or {}
            start, end = loc.get("start") or {}, loc.get("end") or {}
            first, last = int(start.get("line", 0)), int(end.get("line", start.get("line", 0)))
            if not scope.touches(first, last):
                continue
            status = mutant.get("status")
            if status in STRYKER_NOT_RUN:
                continue
            if status in STRYKER_KILLED:
                parsed.killed += 1
            elif status in STRYKER_SURVIVED:
                line = scope.first_touched(list(range(first, last + 1))) or first
                parsed.survivors.append(Survivor(scope.path, line, _stryker_words(mutant, source, start, end)))
            else:
                parsed.unjudged.append(f"{scope.path}:{first} {status}")
                continue
            parsed.count(scope.path)
    return parsed


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
        return Outcome.of(self.name, parse_stryker_json(report.read_text(encoding="utf-8"), job))


# C#: Stryker.NET -------------------------------------------------------------------------------------------------
class StrykerNet(Engine):
    name = "Stryker.NET"
    exts = (".cs",)
    needs = "Stryker.NET (dotnet tool install dotnet-stryker)"
    leftovers = ("StrykerOutput",)

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
        return f"dotnet stryker --since:{quote(job.base)} -r json -O {quote(str(self.out_dir(job)))}"

    def execute(self, job: Job, runner: Runner) -> Outcome:
        self.out_dir(job).mkdir(parents=True, exist_ok=True)  # Stryker.NET wants the output folder to exist
        return super().execute(job, runner)

    def collect(self, job, code, out, runner, started) -> Outcome:
        report = self.out_dir(job) / "reports" / "mutation-report.json"
        if not report.exists():
            return self.no_report(code, out)
        return Outcome.of(self.name, parse_stryker_json(report.read_text(encoding="utf-8"), job))


# Rust: cargo-mutants ---------------------------------------------------------------------------------------------
CARGO_NAME = re.compile(r"^[^:]+:\d+:\d+:\s*")


def parse_cargo_outcomes(text: str, job: Job) -> Parsed:
    """cargo-mutants' mutants.out/outcomes.json. Each mutant has a span (a whole function body for "replace f with
    x"), and counts when that span overlaps a changed line."""
    parsed = Parsed()
    for outcome in json.loads(text).get("outcomes") or []:
        scenario = outcome.get("scenario")
        mutant = scenario.get("Mutant") if isinstance(scenario, dict) else None
        if not mutant:
            continue  # the baseline
        scope = job.scope(mutant.get("file", ""))
        if scope is None:
            continue
        parsed.count(scope.path, 0)
        span = mutant.get("span") or {}
        first = int((span.get("start") or {}).get("line", 0))
        last = int((span.get("end") or {}).get("line", first))
        if not scope.touches(first, last):
            continue
        summary = outcome.get("summary")
        what = CARGO_NAME.sub("", mutant.get("name", "")) or f"replace with {mutant.get('replacement', '?')}"
        if summary == "Unviable":
            continue
        if summary in ("CaughtMutant", "Timeout"):
            parsed.killed += 1
        elif summary == "MissedMutant":
            parsed.survivors.append(Survivor(scope.path, scope.first_touched(list(range(first, last + 1))) or first,
                                             what))
        else:
            parsed.unjudged.append(f"{scope.path}:{first} {summary}")
            continue
        parsed.count(scope.path)
    return parsed


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
        report = job.work / "mutants.out" / "outcomes.json"
        if code == 4:
            return Outcome(self.name, unavailable="the tests fail before any mutation (exit 4)", note=tail(out))
        if code == 0 and not report.exists() and "No mutants to filter" in out:
            # cargo-mutants 27 found no mutant in the diff and wrote no report: an empty report, judged by
            # check_coverage (a comment-only edit passes, changed code without mutants does not)
            return Outcome.of(self.name, Parsed())
        if code not in (0, 2, 3) or not report.exists():
            return self.no_report(code, out)
        return Outcome.of(self.name, parse_cargo_outcomes(report.read_text(encoding="utf-8"), job))


# Go: Gremlins ----------------------------------------------------------------------------------------------------
GREMLINS_SURVIVED = {"LIVED", "NOT COVERED"}
GREMLINS_KILLED = {"KILLED", "TIMED OUT"}
GREMLINS_NOT_RUN = {"NOT VIABLE", "SKIPPED"}
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


def parse_gremlins(text: str, job: Job) -> Parsed:
    """Gremlins' -o JSON: files[].mutations[] with a line, a type and a status. Gremlins gives one line per mutant,
    which counts when it is a changed line. File names are module-relative: an exact match wins, then a single
    changed file ending in that name; two candidates are ambiguous and cannot be judged."""
    parsed = Parsed()
    for entry in json.loads(text).get("files") or []:
        name = job.rel(entry.get("file_name", ""))
        scope = job.scope(name)
        if scope is None:
            candidates = [f for f in job.files if f.path.endswith("/" + name)]
            if len(candidates) > 1:
                parsed.unjudged.append(f"{name} (matches {', '.join(f.path for f in candidates)})")
                continue
            scope = candidates[0] if candidates else None
        if scope is None:
            continue
        parsed.count(scope.path, 0)
        for mutation in entry.get("mutations") or []:
            line = int(mutation.get("line", 0))
            status = mutation.get("status")
            if not scope.touches(line) or status in GREMLINS_NOT_RUN:
                continue
            if status in GREMLINS_KILLED:
                parsed.killed += 1
            elif status in GREMLINS_SURVIVED:
                kind = mutation.get("type", "")
                parsed.survivors.append(Survivor(scope.path, line, GREMLINS_WORDS.get(kind, mutator_words(kind))))
            else:
                parsed.unjudged.append(f"{scope.path}:{line} {status}")
                continue
            parsed.count(scope.path)
    return parsed


class Gremlins(Engine):
    name = "Gremlins"
    exts = (".go",)
    needs = "Gremlins (go install github.com/go-gremlins/gremlins/cmd/gremlins@latest)"

    def available(self, tree: Path) -> str:
        return "" if shutil.which("gremlins") else \
            "not found on PATH; go install github.com/go-gremlins/gremlins/cmd/gremlins@latest"

    def command(self, job: Job) -> str:
        return f"gremlins unleash --diff {quote(job.base)} -o {quote(str(job.work / 'gremlins.json'))}"

    def collect(self, job, code, out, runner, started) -> Outcome:
        report = job.work / "gremlins.json"
        if not report.exists():
            return self.no_report(code, out)
        return Outcome.of(self.name, parse_gremlins(report.read_text(encoding="utf-8"), job))


# Any language: a configured command ------------------------------------------------------------------------------
GENERIC_LINE = re.compile(r"^\s*(?P<path>[^\s:][^:]*):(?P<line>\d+)(?::\d+)?[:\s]\s*(?P<what>.*)$")


class Generic(Engine):
    """[mutants.engines.<ext>] command = "...": exit 0 = no survivors; exit 1 = survivors, each printed as a line
    `path:line what`; exit 1 with no such line on a changed line, any other exit, or not found = unavailable."""
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
            "{base}": quote(job.base), "{head}": quote(job.head or "WORKTREE"), "{diff}": quote(str(job.diff_file)),
        }
        cmd = self.template
        for key, value in values.items():
            cmd = cmd.replace(key, value)
        return cmd

    def collect(self, job, code, out, runner, started) -> Outcome:
        if code == 0:
            return Outcome(self.name)
        if code != 1:
            return Outcome(self.name, unavailable=f"exited {code} (0 = no survivors, 1 = survivors)", note=tail(out))
        survivors = parse_generic(out, job)
        if not survivors:
            return Outcome(self.name, unavailable="exited 1 but listed no survivor on a changed line (print "
                                                  "`path:line what` for each)", note=tail(out))
        return Outcome(self.name, survivors)


def parse_generic(out: str, job: Job) -> list[Survivor]:
    survivors = []
    for line in lines_of(out):
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
    diff_file.write_text(git(tree, "diff", "--no-color", "--no-ext-diff", "-M", *diff_args(scope.base, scope.head),
                             "--", *[f.path for f in files]), encoding="utf-8")
    sources = {}
    for f in files:
        try:
            sources[f.path] = ((tree / f.path).read_bytes().decode("utf-8", errors="replace")
                               if scope.head == WORKTREE else git(tree, "show", f"{scope.head}:{f.path}"))
        except (ScopeError, OSError):
            sources[f.path] = ""
    return Job(tree, scope.base, scope.head, files, diff_file, work, sources)


def tracked_changes(tree: Path) -> set[str]:
    """Tracked files whose working copy differs from the index."""
    try:
        return {p for p in git(tree, "diff", "--name-only", "-z").split("\0") if p}
    except ScopeError:
        return set()


class GuardError(Exception):
    """The tree is not in a state an engine can safely run in; nothing was changed."""


def under(path: str, roots: tuple[str, ...]) -> bool:
    return any(path == r or path.startswith(r.rstrip("/") + "/") for r in roots)


class TreeGuard:
    """Leaves the tree as the engine found it, so `pl done` still sees a clean worktree and old results never enter
    a report. The engine's output paths that already exist are moved aside (`<name>.plowline-aside`) for the run and
    put back afterwards, and what the engine wrote there is removed. A tracked file under those paths that changed is
    checked out again; any other tracked file that changed during the run is left alone and named in a warning,
    because a person or an editor may have changed it."""

    def __init__(self, tree: Path, leftovers: tuple[str, ...]):
        self.tree, self.leftovers = tree, leftovers
        self.warnings: list[str] = []
        self.moved: list[tuple[Path, Path]] = []
        self.existing: set[Path] = set()

    def __enter__(self) -> "TreeGuard":
        stale = [self.aside(self.tree / rel) for rel in self.leftovers if self.aside(self.tree / rel).exists()]
        if stale:
            raise GuardError(f"{', '.join(str(s) for s in stale)} left by an earlier run that was stopped; move it "
                             "back without the .plowline-aside suffix (or delete it), then run again")
        self.dirty = tracked_changes(self.tree)
        try:
            for rel in self.leftovers:
                p = self.tree / rel
                parent = p.parent
                while parent != self.tree and parent != parent.parent:
                    if parent.exists():
                        self.existing.add(parent)
                    parent = parent.parent
                if p.exists():
                    p.rename(self.aside(p))
                    self.moved.append((p, self.aside(p)))
        except OSError as e:
            self.put_back()
            raise GuardError(f"could not move {p} aside before the run ({e}); nothing was changed")
        return self

    @staticmethod
    def aside(p: Path) -> Path:
        return p.with_name(p.name + ".plowline-aside")

    def put_back(self) -> None:
        for p, aside in reversed(self.moved):
            try:
                p.parent.mkdir(parents=True, exist_ok=True)
                aside.rename(p)
            except OSError as e:
                self.warnings.append(f"could not put {p} back (it is at {aside}): {e}")
        self.moved = []

    def __exit__(self, *exc) -> None:
        for rel in self.leftovers:
            p = self.tree / rel
            if p.is_dir():
                shutil.rmtree(p, ignore_errors=True)
            elif p.exists():
                p.unlink(missing_ok=True)
            if p.exists():
                self.warnings.append(f"could not remove {p} (a file in it is still open); remove it before pl done")
            parent = p.parent
            while parent != self.tree and parent not in self.existing and parent.exists() \
                    and not any(parent.iterdir()):
                parent.rmdir()
                parent = parent.parent
        self.put_back()
        changed = sorted(tracked_changes(self.tree) - self.dirty)
        ours = [p for p in changed if under(p, self.leftovers)]
        others = [p for p in changed if p not in ours]
        if ours:
            try:
                git(self.tree, "checkout", "--", *ours)
                self.warnings.append(f"the engine changed tracked file(s) {', '.join(ours)}; checked them out again")
            except ScopeError as e:
                self.warnings.append(f"the engine changed tracked file(s) {', '.join(ours)}: {e}")
        if others:
            self.warnings.append(f"tracked file(s) changed during the run, left as they are: {', '.join(others)}")


def untracked_sources(tree: Path, cfg: Config) -> list[str]:
    """New files `git diff` cannot see, that an engine would otherwise have mutated."""
    try:
        listed = git(tree, "ls-files", "--others", "--exclude-standard", "-z").split("\0")
    except ScopeError:
        return []
    return sorted(p for p in listed if p and not is_test_file(p) and engine_for(Path(p).suffix, cfg) is not None)


def skipped_summary(scope: Scope) -> str:
    counts: dict[str, int] = {}
    for _, why in scope.skipped:
        counts[why] = counts.get(why, 0) + 1
    return ", ".join(f"{n} {why}" for why, n in counts.items())


def run(tree: Path, base: str, head: str, cfg: Config | None = None, env: dict | None = None) -> Result:
    """Mutate the lines base..head added or changed, in `tree`, and judge the survivors. `head` WORKTREE ("") means
    the working tree itself."""
    tree = Path(tree)
    try:
        cfg, scope = prepare(tree, base, head, cfg)
    except (ConfigError, ScopeError) as e:
        return unavailable(str(e))
    if not scope.files:
        line = "MUTANTS: nothing to mutate"
        if scope.skipped:
            line += f" ({_plural(len(scope.skipped), 'changed file')} left out: {skipped_summary(scope)})"
        return Result("pass", line, line)
    runner = Runner(cfg.budget, {"PLOWLINE_BASE": base, "PLOWLINE_HEAD": head, **(env or {})})
    outcomes: list[Outcome] = []
    warnings: list[str] = []
    over_budget = False
    work = Path(tempfile.mkdtemp(prefix="plowline-mutants-"))
    try:
        for n, (engine, files) in enumerate(group_files(scope, cfg)):
            job = write_job(tree, scope, files, work, n)
            guard = TreeGuard(tree, engine.leftovers)
            try:
                with guard:
                    outcomes.append(check_coverage(engine.execute(job, runner), files, job.sources))
            except BudgetExceeded:
                over_budget = True
                break
            except (GuardError, QuoteError) as e:
                outcomes.append(Outcome(engine.name, unavailable=str(e)))
            except Exception as e:  # a report Plowline could not read is not proof against any lane
                outcomes.append(Outcome(engine.name, unavailable=f"crashed: {type(e).__name__}: {e}"))
            finally:
                warnings += guard.warnings
    except ScopeError as e:
        return unavailable(str(e))
    finally:
        shutil.rmtree(work, ignore_errors=True)
    not_mutated = [f"{path} ({why})" for path, why in scope.skipped if why != "test file"]
    if not_mutated:
        warnings.append(f"not mutated: {', '.join(not_mutated)}")
    if head == WORKTREE:
        untracked = untracked_sources(tree, cfg)
        if untracked:
            warnings.append(f"not checked (untracked; git add them to include them): {', '.join(untracked)}")
    return verdict(scope, cfg, outcomes, over_budget, warnings + runner.warnings)


def verdict(scope: Scope, cfg: Config, outcomes: list[Outcome], over_budget: bool = False,
            warnings: list[str] | None = None) -> Result:
    """FAIL when more survivors than max_survivors are proven; otherwise any engine that could not run or judge,
    or the budget running out, makes it UNAVAILABLE; otherwise PASS. Survivors proven by one engine still FAIL when
    another engine could not run."""
    survivors = sorted((s for o in outcomes for s in o.survivors), key=lambda s: (s.path, s.line))
    killed = sum(o.killed for o in outcomes)
    on = f"on {_plural(scope.lines, 'changed line')}"
    lost = [o for o in outcomes if o.unavailable]
    reasons = [f"{o.engine}: {o.unavailable}" for o in lost] + ([budget_message(cfg.budget)] if over_budget else [])
    notes = [o.note for o in outcomes if o.note] + list(warnings or [])
    if len(survivors) > cfg.max_survivors:
        line = f"MUTANTS: FAIL ({_plural(len(survivors), 'survivor')} {on})"
        body, status, notes = survivor_lines(survivors), "fail", reasons + notes
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
    out = [f"scope: {base[:12]}..{head[:12] or 'the working tree'}, {_plural(len(scope.files), 'file')}, "
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
            try:
                cmd = engine.command(job)
            except QuoteError as e:
                out.append(f"  unavailable here: {e}")
                continue
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
