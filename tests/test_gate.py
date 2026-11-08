"""The gate runner: a command or a list of commands, run in order, stop at the first red."""
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from plowline import gate  # noqa: E402

PY = f'"{Path(sys.executable).as_posix()}"'


class GateStepsTest(unittest.TestCase):
    def test_a_string_or_a_list(self):
        self.assertEqual(gate.gate_steps("npm test"), ["npm test"])
        self.assertEqual(gate.gate_steps(["npm run lint", "npm test"]), ["npm run lint", "npm test"])
        self.assertEqual(gate.gate_steps(""), [])
        self.assertEqual(gate.gate_steps([]), [])

    def test_other_shapes_are_refused(self):
        for bad in (3, ["ok", 4], {"cmd": "x"}, ["  "]):
            with self.assertRaises(gate.GateConfigError):
                gate.gate_steps(bad)

    def test_unknown_builtin_is_refused_and_mutants_is_named_as_coming(self):
        with self.assertRaisesRegex(gate.GateConfigError, "no built-in gate named 'nope'"):
            gate.gate_steps(["builtin:nope"])
        with self.assertRaisesRegex(gate.GateConfigError, "0.3"):
            gate.gate_steps("builtin:mutants")


class RunGateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="plowline-gate-"))
        self.ctx = gate.GateContext(tree=self.tmp, base="b" * 40, head="h" * 40, lanes=["one"])

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def step(self, code: int, mark: str) -> str:
        return f"{PY} -c \"open('{mark}', 'w').close(); print('ran {mark}'); raise SystemExit({code})\""

    def test_all_pass(self):
        result = gate.run_gate([self.step(0, "a"), self.step(0, "b")], self.ctx)
        self.assertEqual(result.status, "pass")
        self.assertTrue(result.ok)
        self.assertEqual(result.line, "GATE: PASS (2 steps)")
        self.assertIn("ran a", result.output)
        self.assertIn("ran b", result.output)

    def test_stops_at_the_first_red(self):
        result = gate.run_gate([self.step(0, "a"), self.step(3, "b"), self.step(0, "c")], self.ctx)
        self.assertEqual(result.status, "fail")
        self.assertFalse(result.ok)
        self.assertTrue(result.line.startswith("GATE: FAIL at step 2/3"), result.line)
        self.assertIn("exit 3", result.line)
        self.assertTrue((self.tmp / "b").exists())
        self.assertFalse((self.tmp / "c").exists())

    def test_a_missing_command_is_unavailable_not_red(self):
        result = gate.run_gate(["plowline-no-such-command-here --version"], self.ctx)
        self.assertEqual(result.status, "unavailable", result.output)
        self.assertIn("GATE: UNAVAILABLE", result.line)

    def test_steps_see_the_train_context(self):
        result = gate.run_gate(
            [f"{PY} -c \"import os; print(os.environ['PLOWLINE_LANES'], os.environ['PLOWLINE_BASE'][:3])\""],
            self.ctx)
        self.assertIn("one bbb", result.output)

    def test_a_registered_builtin_runs_with_the_context(self):
        seen = []

        def fake(ctx):
            seen.append(ctx.lanes)
            return 1, "2 mutants survived"
        with mock.patch.dict(gate.BUILTIN_GATES, {"fake": fake}):
            self.assertEqual(gate.gate_steps(["builtin:fake"]), ["builtin:fake"])
            result = gate.run_gate(["builtin:fake"], self.ctx)
        self.assertEqual(seen, [["one"]])
        self.assertEqual(result.status, "fail")
        self.assertIn("2 mutants survived", result.output)


if __name__ == "__main__":
    unittest.main()
