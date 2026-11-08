"""builtin:mutants and pl check: scope from the diff, the engine adapters, the verdict, the gate and the train."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import tomllib
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from plowline import gate, mutants  # noqa: E402
from plowline.demo import ENV  # noqa: E402
from tests.helpers import PY, RepoCase  # noqa: E402

FIX = Path(__file__).resolve().parent / "fixtures" / "mutants"
COUNTER_BASE = "def count_finished(items):\n    return 0\n"


def fixture(*parts: str) -> str:
    return (FIX.joinpath(*parts)).read_text(encoding="utf-8")


def job(files, tree=Path("."), base="b" * 40, head="h" * 40, sources=None):
    return mutants.Job(Path(tree), base, head, files, Path("scope.diff"), Path("work"), sources or {})


def stub(folder: Path, name: str, code: str) -> None:
    """An executable `name` on PATH that runs a Python script (a .cmd shim on Windows)."""
    script = folder / f"{name}_stub.py"
    script.write_text(code, encoding="utf-8")
    if os.name == "nt":
        (folder / f"{name}.cmd").write_text(f'@"{sys.executable}" "{script}" %*\n', encoding="utf-8")
    else:
        exe = folder / name
        exe.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{script}" "$@"\n', encoding="utf-8")
        exe.chmod(0o755)


# A stand-in for mutmut 3: `run` writes the fixture .meta files where mutmut would, `show` prints a fixture diff.
FAKE_MUTMUT = r'''import json, os, pathlib, subprocess, sys, time
args = sys.argv[1:]
log = os.environ.get("FAKE_LOG")
if log:
    with open(log, "a", encoding="utf-8") as f:
        f.write(json.dumps(args) + "\n")
if args[0] == "run":
    sleep = float(os.environ.get("FAKE_SLEEP", "0"))
    if sleep:
        subprocess.Popen([sys.executable, "-c", "import pathlib, sys, time; time.sleep(5); "
                          "pathlib.Path(sys.argv[1]).write_text('alive')", os.environ["FAKE_MARKER"]],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(sleep)
    for target, meta in json.loads(os.environ["FAKE_METAS"]).items():
        out = pathlib.Path("mutants") / (target + ".meta")
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(pathlib.Path(meta).read_text(encoding="utf-8"), encoding="utf-8")
    print("Mutant results")
elif args[0] == "show":
    n = args[1].rsplit("_", 1)[1]
    folder = pathlib.Path(os.environ["FAKE_SHOWS"])
    print((folder / (os.environ.get("FAKE_SHOW_PREFIX", "show_") + n + ".txt")).read_text(encoding="utf-8"), end="")
'''

# A generic engine: survivors are changed lines that say SURVIVOR. {ranges} arrive as quoted path:a-b words.
GENERIC_ENGINE = r'''import pathlib, sys
code = int(sys.argv[1])
found = []
for spec in sys.argv[2:]:
    path, _, span = spec.rpartition(":")
    a, b = map(int, span.split("-"))
    lines = pathlib.Path(path).read_text(encoding="utf-8").splitlines()
    for n in range(a, b + 1):
        if "SURVIVOR" in lines[n - 1]:
            found.append(f"{path}:{n}  kept a SURVIVOR line")
if code:
    sys.exit(code)
print("\n".join(found))
sys.exit(1 if found else 0)
'''


class ScopeTest(unittest.TestCase):
    DIFF = "\n".join([
        "diff --git a/app/a.py b/app/a.py",
        "index 1..2 100644",
        "--- a/app/a.py",
        "+++ b/app/a.py",
        "@@ -3,0 +4,2 @@ def f():",  # added lines 4-5
        "+x",
        "+y",
        "@@ -10 +12 @@",  # modified line 12
        "-old",
        "+new",
        "@@ -20,3 +21,0 @@",  # pure deletion: nothing
        "-gone",
        "@@ -30 +13 @@",  # adjacent to 12: merged
        "-a",
        "+b",
        "diff --git a/old.py b/gone.py",
        "deleted file mode 100644",
        "--- a/gone.py",
        "+++ /dev/null",
        "@@ -1,2 +0,0 @@",
        "-x",
        "diff --git a/lib/old_name.py b/lib/new_name.py",
        "similarity index 80%",
        "rename from lib/old_name.py",
        "rename to lib/new_name.py",
        "--- a/lib/old_name.py",
        "+++ b/lib/new_name.py",
        "@@ -7 +7 @@",
        "-a",
        "+b",
        "diff --git a/same.py b/moved.py",
        "similarity index 100%",
        "rename from same.py",
        "rename to moved.py",
        'diff --git "a/caf\\303\\251.py" "b/caf\\303\\251.py"',
        '--- "a/caf\\303\\251.py"',
        '+++ "b/caf\\303\\251.py"',
        "@@ -0,0 +1 @@",
        "+x",
    ])

    def test_adds_modifies_renames_and_deletions(self):
        files = {f.path: f.ranges for f in mutants.parse_diff(self.DIFF)}
        self.assertEqual(files, {"app/a.py": [(4, 5), (12, 13)], "lib/new_name.py": [(7, 7)], "café.py": [(1, 1)]})

    def test_test_files_are_not_mutated(self):
        for path in ("test_x.py", "pkg/x_test.go", "web/a.test.ts", "web/a.spec.js", "tests/helpers.py",
                     "src/test/Foo.java", "web/__tests__/a.js", "Shop.Tests/CalcTests.cs", "CalcTest.cs"):
            self.assertTrue(mutants.is_test_file(path), path)
        for path in ("src/app.py", "testing.py", "contest/a.py", "latest.go", "src/Calc.cs"):
            self.assertFalse(mutants.is_test_file(path), path)

    def test_skip_rules(self):
        cfg = mutants.Config(skip=["src/legacy", "*.gen.ts"], generated=["schema.py"])
        changed = [mutants.FileScope(p, [(1, 2)]) for p in (
            "src/app.py", "tests/test_app.py", "src/legacy/old.py", "web/api.gen.ts", "db/schema.py", "README.md",
            "web/app.ts")]
        scope = mutants.filter_scope(changed, cfg)
        self.assertEqual([f.path for f in scope.files], ["src/app.py", "web/app.ts"])
        self.assertEqual(dict(scope.skipped), {
            "tests/test_app.py": "test file", "src/legacy/old.py": "[mutants] skip", "web/api.gen.ts": "[mutants] skip",
            "db/schema.py": "generated", "README.md": "no engine for this file type"})
        self.assertEqual(scope.lines, 4)

    def test_a_generic_engine_adds_an_extension(self):
        cfg = mutants.config_from({"mutants": {"engines": {"md": {"command": "lint-mutants {files}"}}}})
        scope = mutants.filter_scope([mutants.FileScope("README.md", [(1, 1)])], cfg)
        self.assertEqual([f.path for f in scope.files], ["README.md"])

    def test_config_is_checked(self):
        cfg = mutants.config_from({"mutants": {"max_survivors": 2, "budget": 30, "skip": "vendor"},
                                   "plowline": {"generated": ["package-lock.json"]}})
        self.assertEqual((cfg.max_survivors, cfg.budget, cfg.skip, cfg.generated),
                         (2, 30, ["vendor"], ["package-lock.json"]))
        for bad in ({"max_survivors": -1}, {"budget": 0}, {"skip": [3]}, {"engines": {"py": {}}}, {"nope": 1}):
            with self.assertRaises(mutants.ConfigError, msg=bad):
                mutants.config_from({"mutants": bad})


class WordsTest(unittest.TestCase):
    def test_a_mutant_in_words(self):
        self.assertEqual(mutants.describe_change("a > 0", "a >= 0"), "changed > to >=")
        self.assertEqual(mutants.describe_change("if not item['done']", "if item['done']"), "removed not")
        self.assertEqual(mutants.describe_change("x = a + b", "x = a - b"), "changed + to -")
        self.assertEqual(mutants.describe_change("save()", ""), "removed save()")
        self.assertEqual(mutants.describe_change("f(a)", "f(a)"), "changed the line")
        self.assertEqual(mutants.mutator_words("CONDITIONALS_NEGATION"), "conditionals negation")
        self.assertEqual(mutants.mutator_words("EqualityOperator"), "equality operator")


class MutmutAdapterTest(unittest.TestCase):
    def counter_job(self, ranges=((2, 3),)):
        return job([mutants.FileScope("tasks/counter.py", list(ranges))],
                   sources={"tasks/counter.py": fixture("mutmut", "counter.py.txt")})

    def test_command_names_one_glob_per_changed_function(self):
        self.assertEqual(mutants.Mutmut().command(self.counter_job()),
                         f"mutmut run {mutants.quote('tasks.counter.x_count_finished__mutmut_*')}")
        shelf = job([mutants.FileScope("src/tasks/shelf.py", [(10, 11)])],
                    sources={"src/tasks/shelf.py": fixture("mutmut", "shelf.py.txt")})
        self.assertEqual(mutants.Mutmut.targets(shelf), ["tasks.shelf.x?Shelf?total__mutmut_*"])
        broken = job([mutants.FileScope("a.py", [(1, 1)])], sources={"a.py": "def (:\n"})
        self.assertEqual(mutants.Mutmut.targets(broken), ["a.*"])
        module_level = job([mutants.FileScope("a.py", [(1, 1)])], sources={"a.py": "X = 1\n\ndef f():\n    pass\n"})
        self.assertEqual(mutants.Mutmut().command(module_level), "")

    def test_parse_real_mutmut_output(self):
        j = self.counter_job()
        globs = mutants.Mutmut.targets(j)
        metas = {"tasks/counter.py": fixture("mutmut", "counter.py.meta")}
        shows = {f"tasks.counter.x_count_finished__mutmut_{n}": fixture("mutmut", f"show_{n}.txt") for n in range(1, 6)}
        survivors, killed = mutants.Mutmut.parse(metas, shows, j.sources, j.files, globs)
        self.assertEqual(killed, 4)
        self.assertEqual([(s.path, s.line, s.what) for s in survivors], [("tasks/counter.py", 3, "removed not")])

    def test_a_mutant_outside_the_changed_lines_is_not_a_survivor(self):
        j = self.counter_job(ranges=((2, 2),))  # only the docstring changed
        metas = {"tasks/counter.py": fixture("mutmut", "counter.py.meta")}
        shows = {"tasks.counter.x_count_finished__mutmut_3": fixture("mutmut", "show_3.txt")}
        survivors, _ = mutants.Mutmut.parse(metas, shows, j.sources, j.files, mutants.Mutmut.targets(j))
        self.assertEqual(survivors, [])

    def test_a_decorated_function_with_a_comment_above_is_placed(self):
        source = "import functools\n\n\n# keeps the cache warm\n@functools.cache\ndef f(a):\n    return a > 0\n"
        self.assertEqual(mutants.python_functions(source), [(None, "f", 5, 7)])
        old, added = mutants.parse_mutmut_show("# m.x_f__mutmut_1: survived\n--- m.py\n+++ m.py\n@@ -1,4 +1,4 @@\n"
                                               " # keeps the cache warm\n @functools.cache\n def f(a):\n"
                                               "-    return a > 0\n+    return a >= 0\n")
        self.assertEqual(mutants.Mutmut._place(source.splitlines(), (5, 7), old), [7])
        self.assertEqual(added, ["    return a >= 0"])

    def test_methods_are_placed_in_their_class(self):
        j = job([mutants.FileScope("tasks/shelf.py", [(10, 11)])],
                sources={"tasks/shelf.py": fixture("mutmut", "shelf.py.txt")})
        globs = mutants.Mutmut.targets(j)
        self.assertEqual(globs, ["tasks.shelf.x?Shelf?total__mutmut_*"])
        metas = {"tasks/shelf.py": fixture("mutmut", "shelf.py.meta")}
        shows = {f"tasks.shelf.xǁShelfǁtotal__mutmut_{n}": fixture("mutmut", f"shelf_show_{n}.txt") for n in (6, 7)}
        survivors, killed = mutants.Mutmut.parse(metas, shows, j.sources, j.files, globs)
        self.assertEqual(killed, 5)  # untouched() and __init__ were not run and are not counted
        self.assertEqual([(s.line, s.what) for s in survivors], [(11, "changed > to >="), (11, "changed 0 to 1")])


class StrykerAdapterTest(unittest.TestCase):
    def test_strykerjs_command_and_report(self):
        j = job([mutants.FileScope("src/price.ts", [(2, 3)]), mutants.FileScope("src/cart.ts", [(5, 5), (9, 12)])])
        ranges = "src/price.ts:2-3,src/cart.ts:5-5,src/cart.ts:9-12"
        self.assertEqual(mutants.StrykerJS().command(j),
                         f"npx --no stryker run --mutate {mutants.quote(ranges)} --reporters json")
        survivors, killed = mutants.parse_stryker_json(fixture("stryker-js", "mutation.json"), j)
        self.assertEqual(killed, 3)  # Killed, Timeout, Killed on lines 2-3
        self.assertEqual([(s.path, s.line, s.what) for s in survivors],
                         [("src/price.ts", 2, "changed > to >="), ("src/price.ts", 2, "block statement: {}")])

    def test_stryker_net_command_and_absolute_paths(self):
        tree = Path(tempfile.mkdtemp(prefix="plowline-snet-"))
        self.addCleanup(shutil.rmtree, tree, ignore_errors=True)
        j = job([mutants.FileScope("src/Calc.cs", [(7, 10)])], tree=tree)
        cmd = mutants.StrykerNet().command(j)
        self.assertTrue(cmd.startswith(f"dotnet stryker --since:{'b' * 40} -r json -O "), cmd)
        text = fixture("stryker-net", "mutation-report.json").replace("__TREE__", tree.resolve().as_posix())
        survivors, killed = mutants.parse_stryker_json(text, j)
        self.assertEqual(killed, 2)
        self.assertEqual([(s.path, s.line, s.what) for s in survivors],
                         [("src/Calc.cs", 7, "changed > to >="), ("src/Calc.cs", 8, "block removal mutation")])


class CargoAndGremlinsTest(unittest.TestCase):
    def test_cargo_mutants(self):
        j = job([mutants.FileScope("src/lib.rs", [(4, 6)])])
        self.assertEqual(mutants.CargoMutants().command(j),
                         f"cargo mutants --in-diff {mutants.quote('scope.diff')} --output {mutants.quote('work')}")
        survivors, killed = mutants.parse_cargo_mutants(
            fixture("cargo-mutants", "missed.txt"), fixture("cargo-mutants", "caught.txt"),
            fixture("cargo-mutants", "timeout.txt"), j)
        self.assertEqual([(s.path, s.line, s.what) for s in survivors],
                         [("src/lib.rs", 5, "replace is_even -> bool with true")])
        self.assertEqual(killed, 3)

    def test_gremlins(self):
        j = job([mutants.FileScope("pkg/cart.go", [(10, 15)])])
        self.assertEqual(mutants.Gremlins().command(j),
                         f"gremlins unleash --diff {'b' * 40} -o {mutants.quote(str(Path('work') / 'gremlins.json'))}")
        survivors, killed = mutants.parse_gremlins(fixture("gremlins", "gremlins.json"), j)
        self.assertEqual(killed, 2)
        self.assertEqual([(s.line, s.what) for s in survivors],
                         [(14, "changed an arithmetic operator (+ to -)"),
                          (15, "changed a comparison boundary (< to <=)")])

    def test_generic_placeholders(self):
        j = job([mutants.FileScope("a b.lua", [(1, 2), (5, 5)])])
        engine = mutants.Generic(".lua", "mut {files} -r {ranges} -b {base} -h {head} -d {diff} {unknown}")
        q = mutants.quote
        self.assertEqual(engine.command(j), f"mut {q('a b.lua')} -r {q('a b.lua:1-2')} {q('a b.lua:5-5')} "
                                            f"-b {'b' * 40} -h {'h' * 40} -d {q('scope.diff')} {{unknown}}")


class VerdictTest(unittest.TestCase):
    def scope(self, lines=3):
        return mutants.Scope("b", "h", [mutants.FileScope("a.py", [(1, lines)])])

    def test_report_lines(self):
        survivors = [mutants.Survivor("a.py", n, "changed < to <=") for n in range(1, 24)]
        r = mutants.verdict(self.scope(), mutants.Config(), [mutants.Outcome("mutmut", survivors, 4)])
        self.assertEqual(r.status, "fail")
        self.assertEqual(r.line, "MUTANTS: FAIL (23 survivors on 3 changed lines)")
        lines = r.report.splitlines()
        self.assertEqual(lines[1], "a.py:1  changed < to <=  (no test failed)")
        self.assertEqual(len(lines), 22)
        self.assertEqual(lines[-1], "... and 3 more")

    def test_pass_and_max_survivors(self):
        r = mutants.verdict(self.scope(1), mutants.Config(), [mutants.Outcome("mutmut", [], 1)])
        self.assertEqual((r.status, r.line), ("pass", "MUTANTS: PASS (1 mutant killed on 1 changed line)"))
        one = [mutants.Survivor("a.py", 2, "removed not")]
        r = mutants.verdict(self.scope(), mutants.Config(max_survivors=1), [mutants.Outcome("mutmut", one, 5)])
        self.assertEqual(r.status, "pass")
        self.assertIn("1 survivor allowed by max_survivors = 1", r.line)

    def test_unavailable_never_fails_open(self):
        r = mutants.verdict(self.scope(), mutants.Config(), [mutants.Outcome("mutmut", [], 3),
                                                             mutants.Outcome("Gremlins", unavailable="not found")])
        self.assertEqual(r.status, "unavailable")
        self.assertEqual(r.line, "MUTANTS: UNAVAILABLE (Gremlins: not found)")
        r = mutants.verdict(self.scope(), mutants.Config(), [mutants.Outcome("mutmut", [], 3)], over_budget=True)
        self.assertEqual(r.line, "MUTANTS: UNAVAILABLE (mutants: budget of 600 s exceeded; raise [mutants] budget "
                                 "or narrow the scope with skip)")

    def test_proven_survivors_still_fail_when_another_engine_is_missing(self):
        r = mutants.verdict(self.scope(), mutants.Config(), [
            mutants.Outcome("mutmut", [mutants.Survivor("a.py", 1, "removed not")]),
            mutants.Outcome("Gremlins", unavailable="not found")])
        self.assertEqual(r.status, "fail")
        self.assertIn("Gremlins: not found", r.report)


class FakeEngineCase(unittest.TestCase):
    """A real git repo with a lane-sized change, and stub engines on PATH."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="plowline-mut-")).resolve()
        self.repo, self.bin = self.tmp / "repo", self.tmp / "bin"
        self.repo.mkdir()
        self.bin.mkdir()
        stub(self.bin, "mutmut", FAKE_MUTMUT)
        self.log = self.tmp / "calls.log"
        self.env = {"PATH": str(self.bin) + os.pathsep + os.environ.get("PATH", ""),
                    "FAKE_METAS": json.dumps({"tasks/counter.py": str(FIX / "mutmut" / "counter.py.meta")}),
                    "FAKE_SHOWS": str(FIX / "mutmut"), "FAKE_LOG": str(self.log),
                    "FAKE_MARKER": str(self.tmp / "grandchild.marker")}
        self.git("init", "-q", "-b", "main")
        self.write("tasks/__init__.py", "")
        self.write("tasks/counter.py", COUNTER_BASE)
        self.base = self.commit("base")
        self.write("tasks/counter.py", fixture("mutmut", "counter.py.txt"))
        self.write("tests/test_counter.py", "def test_counts():\n    assert True\n")
        self.head = self.commit("lane")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def git(self, *args):
        p = subprocess.run(["git", *args], cwd=self.repo, env=ENV, capture_output=True, text=True)
        self.assertEqual(p.returncode, 0, p.stderr)
        return p.stdout.strip()

    def write(self, rel, text):
        (self.repo / rel).parent.mkdir(parents=True, exist_ok=True)
        (self.repo / rel).write_text(text, encoding="utf-8")

    def commit(self, message):
        self.git("add", "-A")
        self.git("commit", "-qm", message)
        return self.git("rev-parse", "HEAD")

    def run_mutants(self, cfg=None, **env):
        with mock.patch.dict(os.environ, {**self.env, **env}):
            return mutants.run(self.repo, self.base, self.head, cfg or mutants.Config())


class EndToEndTest(FakeEngineCase):
    def test_fail_lists_the_survivor_and_cleans_up(self):
        r = self.run_mutants()
        self.assertEqual(r.status, "fail", r.report)
        self.assertEqual(r.line, "MUTANTS: FAIL (1 survivor on 2 changed lines)")
        self.assertIn("tasks/counter.py:3  removed not  (no test failed)", r.report)
        calls = [json.loads(line) for line in self.log.read_text().splitlines()]
        self.assertEqual(calls[0], ["run", "tasks.counter.x_count_finished__mutmut_*"])
        self.assertEqual(calls[1:], [["show", "tasks.counter.x_count_finished__mutmut_3"]])
        self.assertFalse((self.repo / "mutants").exists())  # the engine's folder is removed again

    def test_pass(self):
        meta = json.loads(fixture("mutmut", "counter.py.meta"))
        meta["exit_code_by_key"]["tasks.counter.x_count_finished__mutmut_3"] = 1
        green = self.tmp / "green.meta"
        green.write_text(json.dumps(meta))
        r = self.run_mutants(FAKE_METAS=json.dumps({"tasks/counter.py": str(green)}))
        self.assertEqual((r.status, r.line), ("pass", "MUTANTS: PASS (5 mutants killed on 2 changed lines)"))

    def test_engine_not_found_is_unavailable(self):
        real = shutil.which
        with mock.patch("shutil.which", side_effect=lambda name, *a, **k: None if name == "mutmut" else real(name)):
            r = self.run_mutants()
        self.assertEqual(r.status, "unavailable")
        self.assertIn("mutmut: not found on PATH", r.line)

    def test_budget_exceeded_kills_the_engine_and_is_unavailable(self):
        started = time.monotonic()
        r = self.run_mutants(mutants.Config(budget=2), FAKE_SLEEP="60")
        self.assertLess(time.monotonic() - started, 30)
        self.assertEqual(r.status, "unavailable", r.report)
        self.assertEqual(r.line, "MUTANTS: UNAVAILABLE (mutants: budget of 2 s exceeded; raise [mutants] budget or "
                                 "narrow the scope with skip)")
        time.sleep(6)  # the engine's own child would have written its marker after 5 s
        self.assertFalse(Path(self.env["FAKE_MARKER"]).exists(), "the engine's process tree was not killed")

    def test_nothing_to_mutate(self):
        self.write("tests/test_more.py", "def test_x():\n    pass\n")
        self.write("notes.txt", "hello\n")
        self.head = self.commit("tests only")
        self.base = self.git("rev-parse", "HEAD~1")
        r = self.run_mutants()
        self.assertEqual((r.status, r.report), ("pass", "MUTANTS: nothing to mutate"))

    def test_generic_engine(self):
        self.write("engine.py", GENERIC_ENGINE)
        self.write("app.lua", "a = 1\nb = SURVIVOR\n")
        self.base = self.commit("engine")
        self.write("app.lua", "a = 1\nb = SURVIVOR\nc = 2\n")
        self.head = self.commit("lua lane")
        cmd = f'"{PY}" engine.py {{code}} {{ranges}}'

        def run(code):
            cfg = mutants.config_from({"mutants": {"engines": {"lua": {"command": cmd.replace("{code}", code)}}}})
            return self.run_mutants(cfg)
        self.assertEqual(run("0").line, "MUTANTS: PASS (0 mutants killed on 1 changed line)")
        self.write("app.lua", "a = 1\nb = SURVIVOR\nc = SURVIVOR\n")
        self.head = self.commit("lua lane 2")
        self.base = self.git("rev-parse", "HEAD~2")
        r = run("0")
        self.assertEqual(r.status, "fail", r.report)
        self.assertIn("app.lua:3  kept a SURVIVOR line  (no test failed)", r.report)
        self.assertEqual(run("3").status, "unavailable")
        cfg = mutants.config_from({"mutants": {"engines": {"lua": {"command": "plowline-no-such-engine {files}"}}}})
        missing = self.run_mutants(cfg)
        self.assertEqual(missing.status, "unavailable", missing.report)

    def test_gate_step(self):
        with mock.patch.dict(os.environ, self.env):
            result = gate.run_gate(["builtin:mutants"], gate.GateContext(self.repo, self.base, self.head, ["a"]))
        self.assertEqual(result.status, "fail")
        self.assertEqual(result.line, "GATE: FAIL at step 1/1 `builtin:mutants`: MUTANTS: FAIL (1 survivor on 2 "
                                      "changed lines)")
        self.assertIn("tasks/counter.py:3  removed not", result.output)
        with mock.patch.dict(os.environ, {**self.env, "FAKE_SLEEP": "60"}):
            (self.repo / "plowline.toml").write_text("[mutants]\nbudget = 1\n")
            result = gate.run_gate(["builtin:mutants"], gate.GateContext(self.repo, self.base, self.head))
        self.assertEqual(result.status, "unavailable")
        self.assertTrue(result.line.startswith("GATE: UNAVAILABLE at step 1/1 `builtin:mutants`: MUTANTS: "
                                               "UNAVAILABLE (mutants: budget of 1 s"), result.line)


TXT_ENGINE = '[mutants.engines.txt]\ncommand = \'"{py}" engine.py {code} {{ranges}}\'\n'


class MutantsTrainTest(RepoCase):
    def configure_mutants(self, code="0"):
        (self.app / "engine.py").write_text(GENERIC_ENGINE)
        self.configure(TXT_ENGINE.format(py=PY, code=code), gate='gate = ["builtin:mutants"]')

    def test_a_red_mutants_gate_bounces_the_right_lane(self):
        self.configure_mutants()
        self.lane("a", {"a.txt": "fine\n"})
        self.lane("b", {"b.txt": "SURVIVOR\n"})
        self.lane("c", {"c.txt": "fine too\n"})
        train = self.pl("train")
        self.assertIn("bisection: red b; green a, c", train.stdout, train.stdout + train.stderr)
        self.assertEqual({n: self.state(n) for n in "abc"}, {"a": "landed", "b": "bounced", "c": "landed"})
        notice = (self.app / ".plowline" / "notices" / "b.md").read_text()
        self.assertIn("MUTANTS: FAIL (1 survivor on 2 changed lines)", notice)  # a and b
        self.assertIn("b.txt:1  kept a SURVIVOR line  (no test failed)", notice)

    def test_unavailable_stops_the_train_and_bounces_no_lane(self):
        self.configure_mutants(code="3")
        self.lane("a", {"a.txt": "fine\n"})
        train = self.pl("train")
        self.assertEqual(train.returncode, 2)
        self.assertIn("GATE: UNAVAILABLE at step 1/1 `builtin:mutants`", train.stderr)
        self.assertEqual(self.state("a"), "candidate")

    def test_the_brief_asks_for_pl_check_only_with_builtin_mutants(self):
        self.configure_mutants()
        self.pl("new", "x", "--brief")
        self.assertIn("run `pl check`", (self.app / "briefs" / "x.md").read_text())

    def test_no_pl_check_in_the_brief_without_it(self):
        self.configure()
        self.pl("new", "y", "--brief")
        self.assertNotIn("pl check", (self.app / "briefs" / "y.md").read_text())


class CheckCommandTest(RepoCase):
    def setUp(self):
        super().setUp()
        (self.app / "engine.py").write_text(GENERIC_ENGINE)
        self.configure(TXT_ENGINE.format(py=PY, code="0"))
        self.git("checkout", "-qb", "feature")

    def change(self, text):
        (self.app / "notes.txt").write_text(text)
        self.git("add", "-A")
        self.git("commit", "-qm", "change")

    def test_exit_codes(self):
        self.change("fine\n")
        ok = self.pl("check")
        self.assertEqual(ok.returncode, 0, ok.stdout + ok.stderr)
        self.assertIn("MUTANTS: PASS (0 mutants killed on 1 changed line)", ok.stdout)
        self.change("fine\nSURVIVOR\n")
        red = self.pl("check")
        self.assertEqual(red.returncode, 1, red.stdout + red.stderr)
        self.assertIn("notes.txt:2  kept a SURVIVOR line  (no test failed)", red.stdout)
        toml = self.app / "plowline.toml"
        toml.write_text(toml.read_text().replace("engine.py 0", "engine.py 3"))
        self.git("commit", "-qam", "engine broken")
        down = self.pl("check")
        self.assertEqual(down.returncode, 2, down.stdout + down.stderr)
        self.assertIn("MUTANTS: UNAVAILABLE ([mutants.engines.txt]: exited 3 (0 = no survivors, 1 = survivors))", down.stdout)

    def test_another_ref_and_base(self):
        self.change("SURVIVOR\n")
        self.git("checkout", "-q", "main")
        other = self.pl("check", "feature")
        self.assertEqual(other.returncode, 1, other.stdout + other.stderr)
        self.assertIn("in a temporary worktree", other.stdout)
        same = self.pl("check", "feature", "--base", "feature")
        self.assertEqual((same.returncode, same.stdout.strip().splitlines()[-1]), (0, "MUTANTS: nothing to mutate"))

    def test_explain_prints_scope_and_commands_and_runs_nothing(self):
        self.change("SURVIVOR\n")
        (self.app / "tests").mkdir()
        (self.app / "tests" / "t.txt").write_text("x\n")
        self.git("add", "-A")
        self.git("commit", "-qm", "a test file")
        out = self.pl("check", "--explain")
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertIn("notes.txt  lines 1", out.stdout)
        self.assertIn("skipped tests/t.txt (test file)", out.stdout)
        self.assertIn(f'run: "{PY}" engine.py 0 {mutants.quote("notes.txt:1-1")}', out.stdout)
        self.assertNotIn("MUTANTS:", out.stdout)

    def test_mutmut_is_explained_with_its_function_glob(self):
        (self.app / "tasks").mkdir()
        (self.app / "tasks" / "counter.py").write_text(fixture("mutmut", "counter.py.txt"))
        self.git("add", "-A")
        self.git("commit", "-qm", "py")
        out = self.pl("check", "--explain")
        self.assertIn(f"run: mutmut run {mutants.quote('tasks.counter.x_count_finished__mutmut_*')}", out.stdout)


class DemoCheckTest(unittest.TestCase):
    """The demo's lane f, with a stand-in mutmut on PATH that replays the real mutmut 3.8.1 run of the same code."""

    def test_pl_check_names_the_survivor_on_the_changed_line(self):
        from plowline import demo
        self.assertEqual(demo.COUNTER_LANE, fixture("mutmut", "counter.py.txt"))  # the code the fixture ran on
        folder = Path(tempfile.mkdtemp(prefix="plowline-bin-"))
        self.addCleanup(shutil.rmtree, folder, ignore_errors=True)
        stub(folder, "mutmut", FAKE_MUTMUT)
        env = {"PATH": str(folder) + os.pathsep + os.environ.get("PATH", ""),
               "FAKE_METAS": json.dumps({"tasks/counter.py": str(FIX / "mutmut" / "counter.py.meta")}),
               "FAKE_SHOWS": str(FIX / "mutmut")}
        with mock.patch.dict(os.environ, env):
            result = demo.run(say=lambda _: None)
        self.addCleanup(shutil.rmtree, result["tmp"], ignore_errors=True)
        self.assertEqual(result["check_code"], 1, result["check"])
        self.assertIn("MUTANTS: FAIL (1 survivor on 1 changed line)", result["check"])
        self.assertIn("tasks/counter.py:3  removed not  (no test failed)", result["check"])
        self.assertEqual(result["states"], {"e": "bounced"})


class SetupWritesMutantsTest(RepoCase):
    def test_setup_writes_a_commented_mutants_block(self):
        self.git("commit", "-q", "--allow-empty", "-m", "start")
        p = self.pl("setup", "--yes", "--gate", "echo ok", "--remote", "", "--skip-gate-check", "--no-agent-files")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        text = (self.app / "plowline.toml").read_text()
        self.assertIn("# [mutants]", text)
        self.assertIn("# budget = 600", text)
        self.assertNotIn("mutants", tomllib.loads(text))


if __name__ == "__main__":
    unittest.main()
