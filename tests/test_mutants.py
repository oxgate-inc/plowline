"""builtin:mutants and pl check: scope from the diff, the engine adapters, the verdict, the gate and the train."""
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import tomllib
import unittest
from contextlib import redirect_stderr
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

    def test_an_added_line_that_looks_like_a_header_is_not_one(self):
        diff = "\n".join([
            "diff --git a/y.js b/y.js", "--- a/y.js", "+++ b/y.js",
            "@@ -1 +1,2 @@", "-a", "+++ c", "+--- d",  # added lines "++ c" and "--- d"
            "@@ -9 +10 @@", "-x", "+y"])
        self.assertEqual([(f.path, f.ranges) for f in mutants.parse_diff(diff)], [("y.js", [(1, 2), (10, 10)])])

    def test_test_files_are_not_mutated(self):
        for path in ("test_x.py", "pkg/x_test.go", "web/a.test.ts", "web/a.spec.js", "tests/helpers.py",
                     "src/test/Foo.java", "web/__tests__/a.js", "Shop.Tests/CalcTests.cs", "CalcTest.cs",
                     "app/tests.py", "app/views_tests.py", "conftest.py", "src/conftest.py", "Shop/CalcSpec.cs",
                     "web/login.cy.ts", "e2e/login.ts"):
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
        parsed = mutants.Mutmut.parse(metas, shows, j.sources, j.files, globs)
        self.assertEqual(parsed.killed, 4)
        self.assertEqual([(s.path, s.line, s.what) for s in parsed.survivors], [("tasks/counter.py", 3, "removed not")])
        self.assertEqual(parsed.evaluated, {"tasks/counter.py": 5})

    def test_a_mutant_outside_the_changed_lines_is_not_a_survivor(self):
        j = self.counter_job(ranges=((2, 2),))  # only the docstring changed
        metas = {"tasks/counter.py": fixture("mutmut", "counter.py.meta")}
        shows = {"tasks.counter.x_count_finished__mutmut_3": fixture("mutmut", "show_3.txt")}
        parsed = mutants.Mutmut.parse(metas, shows, j.sources, j.files, mutants.Mutmut.targets(j))
        self.assertEqual(parsed.survivors, [])

    def test_a_package_init_is_named_like_mutmut_names_it(self):
        # mutmut 3 drops `.__init__.` from mutant names: pkg/__init__.py -> pkg.x_f__mutmut_1
        self.assertEqual(mutants.mutmut_module("pkg/__init__.py"), "pkg")
        self.assertEqual(mutants.mutmut_module("src/pkg/sub/__init__.py"), "pkg.sub")
        self.assertEqual(mutants.mutmut_module("src/pkg/mod.py"), "pkg.mod")
        j = job([mutants.FileScope("pkg/__init__.py", [(2, 2)])], sources={"pkg/__init__.py": "def f(a):\n    return a\n"})
        self.assertEqual(mutants.Mutmut.targets(j), ["pkg.x_f__mutmut_*"])

    def test_results_that_name_no_mutant_of_the_file_are_not_a_pass(self):
        j = self.counter_job()
        renamed = fixture("mutmut", "counter.py.meta").replace("tasks.counter.", "tasks.other.")
        parsed = mutants.Mutmut.parse({"tasks/counter.py": renamed}, {}, j.sources, j.files, mutants.Mutmut.targets(j))
        outcome = mutants.check_coverage(mutants.Outcome.of("mutmut", parsed), j.files)
        self.assertEqual(outcome.unavailable, "its report has nothing for tasks/counter.py")

    def test_an_unknown_mutmut_status_is_unavailable(self):
        j = self.counter_job()
        meta = json.loads(fixture("mutmut", "counter.py.meta"))
        meta["exit_code_by_key"]["tasks.counter.x_count_finished__mutmut_2"] = 35  # suspicious
        meta["exit_code_by_key"]["tasks.counter.x_count_finished__mutmut_4"] = None  # not checked
        parsed = mutants.Mutmut.parse({"tasks/counter.py": json.dumps(meta)}, {}, j.sources, j.files,
                                      mutants.Mutmut.targets(j))
        outcome = mutants.Outcome.of("mutmut", parsed)
        self.assertIn("could not judge 2 mutant(s)", outcome.unavailable)
        self.assertIn("x_count_finished__mutmut_2: exit 35", outcome.unavailable)

    def test_a_decorated_function_with_a_comment_above_is_placed(self):
        source = "import functools\n\n\n# keeps the cache warm\n@functools.cache\ndef f(a):\n    return a > 0\n"
        self.assertEqual(mutants.python_functions(source), [(None, "f", 5, 7)])
        old, added = mutants.parse_mutmut_show("# m.x_f__mutmut_1: survived\n--- m.py\n+++ m.py\n@@ -1,4 +1,4 @@\n"
                                               " # keeps the cache warm\n @functools.cache\n def f(a):\n"
                                               "-    return a > 0\n+    return a >= 0\n")
        self.assertEqual(mutants.Mutmut._place(source.splitlines(), (5, 7), old), [7])
        self.assertEqual(added, ["    return a >= 0"])

    def test_an_ambiguous_place_falls_back_to_the_function(self):
        source = "def f(a):\n    x = a + 1\n    y = 2\n    x = a + 1\n    return x\n".split("\n")[:-1]
        self.assertIsNone(mutants.Mutmut._place(source, (1, 5), [("-", "    x = a + 1")]))
        self.assertEqual(mutants.Mutmut._place(source, (1, 5), [("-", "    y = 2")]), [3])

    def test_lines_are_split_on_newline_only(self):
        self.assertEqual(mutants.lines_of("a\x0cb c\nd\r\ne\n"), ["a\x0cb c", "d", "e"])
        source = "def f(a):\n    s = 'x\x0cy'\n    return a > 0\n"
        functions = mutants.python_functions(source)
        self.assertEqual(functions, [(None, "f", 1, 3)])
        self.assertEqual(mutants.Mutmut._place(mutants.lines_of(source), (1, 3), [("-", "    return a > 0")]), [3])

    def test_methods_are_placed_in_their_class(self):
        j = job([mutants.FileScope("tasks/shelf.py", [(10, 11)])],
                sources={"tasks/shelf.py": fixture("mutmut", "shelf.py.txt")})
        globs = mutants.Mutmut.targets(j)
        self.assertEqual(globs, ["tasks.shelf.x?Shelf?total__mutmut_*"])
        metas = {"tasks/shelf.py": fixture("mutmut", "shelf.py.meta")}
        shows = {f"tasks.shelf.xǁShelfǁtotal__mutmut_{n}": fixture("mutmut", f"shelf_show_{n}.txt") for n in (6, 7)}
        parsed = mutants.Mutmut.parse(metas, shows, j.sources, j.files, globs)
        self.assertEqual(parsed.killed, 5)  # untouched() and __init__ were not run and are not counted
        self.assertEqual([(s.line, s.what) for s in parsed.survivors], [(11, "changed > to >="), (11, "changed 0 to 1")])


class StrykerAdapterTest(unittest.TestCase):
    def test_strykerjs_command_and_report(self):
        j = job([mutants.FileScope("src/price.ts", [(2, 3)]), mutants.FileScope("src/cart.ts", [(5, 5), (9, 12)])])
        ranges = "src/price.ts:2-3,src/cart.ts:5-5,src/cart.ts:9-12"
        self.assertEqual(mutants.StrykerJS().command(j),
                         f"npx --no stryker run --mutate {mutants.quote(ranges)} --reporters json")
        parsed = mutants.parse_stryker_json(fixture("stryker-js", "mutation.json"), j)
        self.assertEqual(parsed.killed, 3)  # Killed, Timeout, Killed on lines 2-3
        self.assertEqual([(s.path, s.line, s.what) for s in parsed.survivors],
                         [("src/price.ts", 2, "changed > to >="), ("src/price.ts", 2, "block statement: {}")])
        outcome = mutants.check_coverage(mutants.Outcome.of("StrykerJS", parsed), j.files)
        self.assertEqual(outcome.unavailable, "its report has nothing for src/cart.ts")

    def test_stryker_net_command_and_absolute_paths(self):
        tree = Path(tempfile.mkdtemp(prefix="plowline-snet-"))
        self.addCleanup(shutil.rmtree, tree, ignore_errors=True)
        j = job([mutants.FileScope("src/Calc.cs", [(7, 10)])], tree=tree)
        cmd = mutants.StrykerNet().command(j)
        self.assertTrue(cmd.startswith(f"dotnet stryker --since:{mutants.quote('b' * 40)} -r json -O "), cmd)
        text = fixture("stryker-net", "mutation-report.json").replace("__TREE__", tree.resolve().as_posix())
        parsed = mutants.parse_stryker_json(text, j)
        self.assertEqual(parsed.killed, 2)
        self.assertEqual([(s.path, s.line, s.what) for s in parsed.survivors],
                         [("src/Calc.cs", 7, "changed > to >="), ("src/Calc.cs", 8, "block removal mutation")])
        elsewhere = fixture("stryker-net", "mutation-report.json").replace("__TREE__", "/some/other/checkout")
        outcome = mutants.check_coverage(mutants.Outcome.of("Stryker.NET", mutants.parse_stryker_json(elsewhere, j)),
                                         j.files)
        self.assertEqual(outcome.unavailable, "its report has nothing for src/Calc.cs")


class CargoAndGremlinsTest(unittest.TestCase):
    """cargo-mutants 27.1.0, a real run: shipping() gained an `if` on lines 4-6 (see fixtures/.../scope.diff)."""

    def test_cargo_mutants_real_report(self):
        j = job([mutants.FileScope("src/lib.rs", [(4, 6)])])
        self.assertEqual(mutants.CargoMutants().command(j),
                         f"cargo mutants --in-diff {mutants.quote('scope.diff')} --output {mutants.quote('work')}")
        self.assertIn("+    if weight > 10 {", fixture("cargo-mutants", "scope.diff"))  # the diff it ran with
        parsed = mutants.parse_cargo_outcomes(fixture("cargo-mutants", "outcomes.json"), j)
        self.assertEqual([(s.line, s.what) for s in parsed.survivors],
                         [(4, "replace > with == in shipping"), (4, "replace > with >= in shipping"),
                          (5, "replace * with + in shipping"), (5, "replace * with / in shipping")])
        self.assertEqual(parsed.killed, 3)  # the two body replacements (span 3-7) and `>` to `<`

    def test_a_function_body_mutant_counts_on_any_changed_line_of_its_span(self):
        # "replace shipping -> u32 with 0" starts on line 3 and spans 3-7; a lane that changed only line 6 still owns
        # it. Here it is made a survivor (the real run caught it) to show it is not dropped.
        j = job([mutants.FileScope("src/lib.rs", [(6, 6)])])
        report = json.loads(fixture("cargo-mutants", "outcomes.json"))
        for outcome in report["outcomes"]:
            mutant = (outcome["scenario"] or {}).get("Mutant") if isinstance(outcome["scenario"], dict) else None
            if mutant and mutant["genre"] == "FnValue":
                outcome["summary"] = "MissedMutant"
        parsed = mutants.parse_cargo_outcomes(json.dumps(report), j)
        self.assertEqual([(s.line, s.what) for s in parsed.survivors],
                         [(6, "replace shipping -> u32 with 0"), (6, "replace shipping -> u32 with 1")])
        self.assertEqual(parsed.killed, 0)
        self.assertEqual(missed_lines(fixture("cargo-mutants", "missed.txt")), [4, 4, 5, 5])  # missed.txt: start only

    def test_cargo_mutants_with_no_mutant_in_the_diff(self):
        # cargo-mutants 27.1.0 on a comment-only diff: "INFO No mutants to filter", exit 0, no outcomes.json
        work = Path(tempfile.mkdtemp(prefix="plowline-cargo-"))
        self.addCleanup(shutil.rmtree, work, ignore_errors=True)
        source = fixture("cargo-mutants", "lib.rs.txt")
        engine, out = mutants.CargoMutants(), "INFO No mutants to filter\n"
        comment = job([mutants.FileScope("src/lib.rs", [(1, 1)])], sources={"src/lib.rs": source})
        comment.work = work
        result = mutants.check_coverage(engine.collect(comment, 0, out, None, 0), comment.files, comment.sources)
        self.assertEqual(result.unavailable, "")
        code = job([mutants.FileScope("src/lib.rs", [(10, 10)])], sources={"src/lib.rs": source})
        code.work = work
        result = mutants.check_coverage(engine.collect(code, 0, out, None, 0), code.files, code.sources)
        self.assertEqual(result.unavailable, "its report has nothing for src/lib.rs")
        self.assertIn("exited 0 without a report", engine.collect(code, 0, "something else\n", None, 0).unavailable)

    def test_gremlins(self):
        j = job([mutants.FileScope("pkg/cart.go", [(10, 15)])])
        self.assertEqual(mutants.Gremlins().command(j), f"gremlins unleash --diff {mutants.quote('b' * 40)} "
                                                        f"-o {mutants.quote(str(Path('work') / 'gremlins.json'))}")
        parsed = mutants.parse_gremlins(fixture("gremlins", "gremlins.json"), j)
        self.assertEqual(parsed.killed, 2)
        self.assertEqual([(s.line, s.what) for s in parsed.survivors],
                         [(14, "changed an arithmetic operator (+ to -)"),
                          (15, "changed a comparison boundary (< to <=)")])

    def test_gremlins_ambiguous_names_are_not_judged(self):
        text = json.dumps({"files": [{"file_name": "cart.go", "mutations": [
            {"line": 3, "column": 1, "type": "ARITHMETIC_BASE", "status": "LIVED"}]}]})
        j = job([mutants.FileScope("a/cart.go", [(3, 3)]), mutants.FileScope("b/cart.go", [(3, 3)])])
        self.assertIn("cart.go (matches a/cart.go, b/cart.go)", mutants.parse_gremlins(text, j).unjudged)
        one = job([mutants.FileScope("svc/cart.go", [(3, 3)])])
        self.assertEqual([s.path for s in mutants.parse_gremlins(text, one).survivors], ["svc/cart.go"])

    def test_generic_placeholders_are_all_quoted(self):
        j = job([mutants.FileScope("a b.lua", [(1, 2), (5, 5)])], base="a;b", head="h h")
        engine = mutants.Generic(".lua", "mut {files} -r {ranges} -b {base} -h {head} -d {diff} {unknown}")
        q = mutants.quote
        self.assertEqual(engine.command(j), f"mut {q('a b.lua')} -r {q('a b.lua:1-2')} {q('a b.lua:5-5')} "
                                            f"-b {q('a;b')} -h {q('h h')} -d {q('scope.diff')} {{unknown}}")

    def echo(self, value: str) -> str:
        p = subprocess.run(f"\"{PY}\" -c \"import sys; print(sys.argv[1])\" {mutants.quote(value)}", shell=True,
                           capture_output=True, text=True)
        return p.stdout.rstrip("\n") + p.stderr

    def test_quoted_arguments_survive_the_shell(self):
        for value in ("50% off %PATH% a;b $HOME 'x' & | < >", "C:\\some dir\\", "a\\%b\\\\", "x\\\\\\"):
            self.assertEqual(self.echo(value), value)

    def test_a_double_quote_is_refused_on_windows(self):
        value = 'a"&echo PWNED&"b'
        if os.name == "nt":
            with self.assertRaisesRegex(mutants.QuoteError, "refusing to quote"):
                mutants.quote(value)
            j = job([mutants.FileScope("a.lua", [(1, 1)])], base=value)
            with self.assertRaises(mutants.QuoteError):
                mutants.Generic(".lua", "mut {base}").command(j)
        else:
            self.assertEqual(self.echo(value), value)


class CoverageTest(unittest.TestCase):
    """A file whose changed lines hold code but got no judged mutant is UNAVAILABLE; a comment-only edit passes."""

    def outcome(self, evaluated):
        return mutants.Outcome("engine", evaluated=evaluated)

    def test_code_lines(self):
        rs = "use std::fmt;\n// a comment\n\n/// doc\nfn f() -> u32 {\n    1\n}\n"
        self.assertEqual(mutants.code_lines("src/a.rs", rs, [(1, 7)]), [5, 6])
        go = 'import (\n\t"fmt"\n\tlog "log"\n)\n// c\nfunc f() int {\n\treturn 1\n}\n'
        self.assertEqual(mutants.code_lines("a.go", go, [(1, 8)]), [6, 7])
        py = "import os\nTIMEOUT = 30\n\n\ndef f(a):\n    # why\n    return a > 0\n"
        self.assertEqual(mutants.code_lines("a.py", py, [(1, 7)]), [5, 7])  # module level is not mutmut's
        ts = "import { x } from './x';\nexport { y } from './y';\n/* block\n * more\n */\nconst z = x + 1;\n"
        self.assertEqual(mutants.code_lines("a.ts", ts, [(1, 6)]), [6])
        cs = "using System;\nnamespace Shop;\n"
        self.assertEqual(mutants.code_lines("A.cs", cs, [(1, 2)]), [2])
        self.assertEqual(mutants.code_lines("a.ts", "", [(3, 4)]), [3, 4])  # source unknown: assume code

    def test_a_star_is_a_comment_only_inside_a_block(self):
        rs = "fn f(c: &mut u32) {\n    *c += 1;\n    /* x */ foo();\n    /* a\n     * b\n     */\n}\n"
        self.assertEqual(mutants.code_lines("src/a.rs", rs, [(2, 6)]), [2, 3])  # deref and code after `*/`
        self.assertEqual(mutants.code_lines("a.go", "func f(p *int) {\n\t*p = 1\n}\n", [(2, 2)]), [2])
        self.assertEqual(mutants.code_lines("a.ts", 'const s = "/* not */";\nlet x = 1;\n', [(2, 2)]), [2])
        self.assertEqual(mutants.code_lines("a.ts", "/*\nlet x = 1;\n*/\n", [(2, 2)]), [])  # inside a block

    def test_a_python_file_that_does_not_parse_counts_its_lines(self):
        self.assertEqual(mutants.code_lines("a.py", "def f(:\n    # why\n    return 1\nX = 2\n", [(1, 4)]),
                         [1, 3, 4])
        out = mutants.check_coverage(self.outcome({}), [mutants.FileScope("a.py", [(1, 1)])], {"a.py": "def f(:\n"})
        self.assertEqual(out.unavailable, "its report has nothing for a.py")

    def test_a_docstring_is_not_code(self):
        py = 'def f(a):\n    """How it works,\n    in two lines."""\n    return a\n'
        self.assertEqual(mutants.code_lines("a.py", py, [(2, 4)]), [4])
        out = mutants.check_coverage(self.outcome({"a.py": 0}), [mutants.FileScope("a.py", [(2, 3)])], {"a.py": py})
        self.assertEqual(out.unavailable, "")

    def test_a_comment_only_edit_passes_even_when_the_report_omits_the_file(self):
        files = [mutants.FileScope("src/lib.rs", [(2, 2)])]
        out = mutants.check_coverage(self.outcome({}), files, {"src/lib.rs": "fn f() {}\n// tidy\n"})
        self.assertEqual(out.unavailable, "")
        self.assertIn("only comments, docstrings, blank lines or imports changed in src/lib.rs", out.note)

    def test_changed_code_with_no_judged_mutant_is_unavailable(self):
        files = [mutants.FileScope("a.ts", [(2, 2)])]
        out = mutants.check_coverage(self.outcome({"a.ts": 0}), files, {"a.ts": "// x\nconst z = x + 1;\n"})
        self.assertEqual(out.unavailable, "no mutant was judged on the changed code of a.ts (line 2)")
        out = mutants.check_coverage(self.outcome({}), files, {"a.ts": "// x\nconst z = x + 1;\n"})
        self.assertEqual(out.unavailable, "its report has nothing for a.ts")

    def test_stryker_ignored_or_uncompiled_mutants_are_not_a_pass(self):
        report = json.loads(fixture("stryker-js", "mutation.json"))
        for mutant in report["files"]["src/price.ts"]["mutants"]:
            loc = mutant["location"]
            if loc["start"]["line"] <= 3 <= loc["end"]["line"]:  # every mutant touching line 3
                mutant["status"] = "CompileError" if mutant["mutatorName"] == "BlockStatement" else "Ignored"
        j = job([mutants.FileScope("src/price.ts", [(3, 3)])],
                sources={"src/price.ts": fixture("stryker-js", "price.ts.txt")})
        out = mutants.check_coverage(mutants.Outcome.of("StrykerJS", mutants.parse_stryker_json(json.dumps(report), j)),
                                     j.files, j.sources)
        self.assertEqual(out.unavailable, "no mutant was judged on the changed code of src/price.ts (line 3)")


class GuardTest(unittest.TestCase):
    def setUp(self):
        self.tree = Path(tempfile.mkdtemp(prefix="plowline-guard-")).resolve()
        self.addCleanup(shutil.rmtree, self.tree, ignore_errors=True)
        for args in (("init", "-q", "-b", "main"),):
            subprocess.run(["git", *args], cwd=self.tree, env=ENV, check=True, capture_output=True)
        (self.tree / "reports" / "mutation").mkdir(parents=True)
        (self.tree / "reports" / "mutation" / "mutation.json").write_text("committed\n")
        (self.tree / "src.ts").write_text("one\n")
        subprocess.run(["git", "add", "-A"], cwd=self.tree, env=ENV, check=True)
        subprocess.run(["git", "commit", "-qm", "c"], cwd=self.tree, env=ENV, check=True)
        (self.tree / ".stryker-tmp").mkdir()
        (self.tree / ".stryker-tmp" / "mine.txt").write_text("mine\n")

    def test_engine_output_is_put_back_and_other_edits_are_kept(self):
        guard = mutants.TreeGuard(self.tree, mutants.StrykerJS.leftovers)
        with guard:
            (self.tree / "reports" / "mutation" / "mutation.json").write_text("engine output\n")
            (self.tree / ".stryker-tmp").mkdir()
            (self.tree / ".stryker-tmp" / "sandbox.txt").write_text("engine\n")
            (self.tree / "src.ts").write_text("edited by a person\n")
        self.assertEqual((self.tree / "reports" / "mutation" / "mutation.json").read_text(), "committed\n")
        self.assertEqual(sorted(p.name for p in (self.tree / ".stryker-tmp").iterdir()), ["mine.txt"])
        self.assertEqual((self.tree / "src.ts").read_text(), "edited by a person\n")
        self.assertEqual(guard.warnings, ["tracked file(s) changed during the run, left as they are: src.ts"])

    def test_a_failed_move_aside_puts_back_what_it_moved(self):
        real = Path.rename

        def rename(self_path, target):
            if self_path.name == ".stryker-tmp":
                raise PermissionError("a file in it is open")
            return real(self_path, target)
        with mock.patch.object(Path, "rename", rename):
            with self.assertRaisesRegex(mutants.GuardError, "could not move .*stryker-tmp aside"):
                with mutants.TreeGuard(self.tree, mutants.StrykerJS.leftovers):
                    self.fail("the engine must not run")
        self.assertEqual((self.tree / "reports" / "mutation" / "mutation.json").read_text(), "committed\n")
        self.assertEqual(list(self.tree.rglob("*.plowline-aside")), [])


class InterruptTest(unittest.TestCase):
    def test_a_failed_kill_on_ctrl_c_is_reported(self):
        proc = mock.Mock(pid=4242)
        proc.communicate.side_effect = KeyboardInterrupt
        err = io.StringIO()
        with mock.patch("subprocess.Popen", return_value=proc), \
                mock.patch.object(mutants, "kill_tree", return_value="taskkill /T exited 128"), \
                redirect_stderr(err), self.assertRaises(KeyboardInterrupt):
            mutants.Runner(60).run("engine --go", Path("."))
        self.assertIn("the engine may still be running (taskkill /T exited 128); stop it by hand: engine --go",
                      err.getvalue())


def missed_lines(text: str) -> list[int]:
    return [int(line.split(":")[1]) for line in text.splitlines() if line.strip()]


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
        self.assertEqual((r.status, r.report), ("pass", "MUTANTS: nothing to mutate (2 changed files left out: "
                                                        "1 no engine for this file type, 1 test file)"))

    def test_a_corrupt_report_is_unavailable_not_fail(self):
        corrupt = self.tmp / "corrupt.meta"
        corrupt.write_text('{"exit_code_by_key": {"tasks.counter.x_count_')
        r = self.run_mutants(FAKE_METAS=json.dumps({"tasks/counter.py": str(corrupt)}))
        self.assertEqual(r.status, "unavailable", r.report)
        self.assertIn("mutmut: crashed: JSONDecodeError", r.line)

    def test_results_for_another_module_are_unavailable_not_a_pass(self):
        other = self.tmp / "other.meta"
        other.write_text(fixture("mutmut", "counter.py.meta").replace("tasks.counter.", "tasks.elsewhere."))
        r = self.run_mutants(FAKE_METAS=json.dumps({"tasks/counter.py": str(other)}))
        self.assertEqual(r.line, "MUTANTS: UNAVAILABLE (mutmut: its report has nothing for tasks/counter.py)")

    def test_a_targeted_file_without_fresh_results_is_unavailable(self):
        self.write("tasks/shelf.py", fixture("mutmut", "shelf.py.txt"))
        self.head = self.commit("two files")
        self.base = self.git("rev-parse", "HEAD~2")
        r = self.run_mutants()  # the stand-in writes results for counter.py only
        self.assertEqual(r.status, "unavailable", r.report)
        self.assertIn("mutmut: no fresh results for tasks/shelf.py", r.line)

    def test_earlier_engine_output_is_moved_aside_and_put_back(self):
        old = self.repo / "mutants" / "tasks" / "counter.py.meta"
        meta = json.loads(fixture("mutmut", "counter.py.meta"))
        meta["exit_code_by_key"]["tasks.counter.x_count_finished__mutmut_3"] = 1  # an old, green result
        self.write("mutants/tasks/counter.py.meta", json.dumps(meta))
        self.write("mutants/keep.txt", "mine\n")
        r = self.run_mutants()
        self.assertEqual(r.status, "fail", r.report)  # the fresh run's survivor, not the old green result
        self.assertEqual(json.loads(old.read_text())["exit_code_by_key"]["tasks.counter.x_count_finished__mutmut_3"], 1)
        self.assertEqual((self.repo / "mutants" / "keep.txt").read_text(), "mine\n")
        self.assertFalse((self.repo / "mutants.plowline-aside").exists())

    def test_a_tracked_file_edited_during_the_run_survives(self):
        # Someone (an editor, an agent, a person) saves data.json while the engine runs: it is theirs, not the engine's.
        self.write("engine.py", "import pathlib\npathlib.Path('data.json').write_text('edited meanwhile')\n")
        self.write("data.json", "{}\n")
        self.write("app.lua", "a = 1\n")
        self.base = self.commit("engine")
        self.write("app.lua", "a = 2\n")
        self.head = self.commit("lua")
        cfg = mutants.config_from({"mutants": {"engines": {"lua": {"command": f'"{PY}" engine.py'}}}})
        r = self.run_mutants(cfg)
        self.assertEqual(r.status, "pass", r.report)
        self.assertEqual((self.repo / "data.json").read_text(), "edited meanwhile")
        self.assertIn("tracked file(s) changed during the run, left as they are: data.json", r.report)

    def test_a_stale_aside_stops_the_run_and_touches_nothing(self):
        self.write("mutants/mine.txt", "current\n")
        self.write("mutants.plowline-aside/old.txt", "from a killed run\n")
        r = self.run_mutants()
        self.assertEqual(r.status, "unavailable", r.report)
        self.assertIn("mutants.plowline-aside left by an earlier run that was stopped", r.line)
        self.assertEqual((self.repo / "mutants" / "mine.txt").read_text(), "current\n")
        self.assertEqual((self.repo / "mutants.plowline-aside" / "old.txt").read_text(), "from a killed run\n")
        self.assertFalse(self.log.exists())  # the engine never ran

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
        crashed = run("1")  # exit 1 with no `path:line what` line: a crash, not survivors
        self.assertEqual(crashed.status, "unavailable", crashed.report)
        self.assertIn("exited 1 but listed no survivor on a changed line", crashed.line)
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
        self.assertEqual((same.returncode, same.stdout.strip()),
                         (0, "MUTANTS: nothing to mutate (feature has no commits beyond feature)"))

    def test_on_the_trunk_itself(self):
        self.git("checkout", "-q", "main")
        out = self.pl("check")
        self.assertEqual((out.returncode, out.stdout.strip()), (0, "MUTANTS: nothing to mutate (HEAD has no commits "
                                                                   "beyond main)"))

    def test_uncommitted_changes_are_refused_unless_dirty(self):
        self.change("fine\n")
        (self.app / "notes.txt").write_text("fine\nSURVIVOR\n")
        refused = self.pl("check")
        self.assertEqual(refused.returncode, 2, refused.stdout + refused.stderr)
        self.assertIn("Commit or stash them first, or pass --dirty", refused.stderr)
        (self.app / "brand-new.txt").write_text("SURVIVOR\n")  # untracked: git diff cannot see it
        dirty = self.pl("check", "--dirty")
        self.assertIn("not checked (untracked; git add them to include them): brand-new.txt", dirty.stdout)
        self.assertEqual(dirty.returncode, 1, dirty.stdout + dirty.stderr)
        self.assertIn("pl check: the working tree against main", dirty.stdout)
        self.assertIn("notes.txt:2  kept a SURVIVOR line  (no test failed)", dirty.stdout)  # uncommitted line, in scope
        self.assertIn("lines 1-2", self.pl("check", "--dirty", "--explain").stdout)

    def test_a_crash_exits_2_not_1(self):
        self.change("fine\n")
        with mock.patch("plowline.mutants.run", side_effect=ValueError("bad report")):
            code, out = self.in_process("check")
        self.assertEqual(code, 2, out)
        self.assertIn("MUTANTS: UNAVAILABLE (crashed: ValueError: bad report)", out)

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
