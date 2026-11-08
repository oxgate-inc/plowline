"""The gate: the commands that must pass on the merged train before anything lands.

`gate` is one command or a list of commands. They run in order on the merged tree and the first red one stops
the run. A step written `builtin:<name>` runs a check that ships with Plowline instead of a shell command;
none ships yet, and `builtin:mutants` (a diff-scoped mutation test) is planned for 0.3.

The verdict comes from the exit codes this runner captured itself, never from a tail of the output, and every
run ends with one completion line: GATE: PASS, GATE: FAIL or GATE: UNAVAILABLE. A command that could not start
at all (not found, not executable) is UNAVAILABLE: a red result is not proof the check ran, so the train
blames no lane for it.
"""
from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

NOT_FOUND = {127, 9009}  # sh and cmd.exe exit codes for "command not found"
NOT_RECOGNIZED = "is not recognized as an internal or external command"  # cmd.exe exits 1 with this line
COMING = {"mutants": "0.3"}


@dataclass
class GateContext:
    tree: Path  # the merged tree the gate runs in
    base: str  # the trunk commit the train started from
    head: str  # the merged commit under test
    lanes: list[str] = field(default_factory=list)


@dataclass
class GateResult:
    status: str  # pass | fail | unavailable
    line: str  # the completion line
    output: str = ""

    @property
    def ok(self) -> bool:
        return self.status == "pass"


# name -> check(ctx) -> (exit code, output). Registered by Plowline itself, never from configuration.
BUILTIN_GATES: dict[str, Callable[[GateContext], tuple[int, str]]] = {}


class GateConfigError(Exception):
    pass


def gate_steps(value) -> list[str]:
    """The gate's steps from plowline.toml: a string, or a list of strings."""
    if isinstance(value, str):
        steps = [value] if value.strip() else []
    elif isinstance(value, list) and all(isinstance(s, str) and s.strip() for s in value):
        steps = list(value)
    else:
        raise GateConfigError("gate must be a command, or a list of commands, e.g. gate = [\"npm run lint\", \"npm test\"]")
    for step in steps:
        if step.startswith("builtin:"):
            name = step.removeprefix("builtin:").strip()
            if name not in BUILTIN_GATES:
                soon = f" (it is planned for Plowline {COMING[name]})" if name in COMING else ""
                raise GateConfigError(f"gate step {step!r}: no built-in gate named {name!r}{soon}")
    return steps


def run_gate(steps: list[str], ctx: GateContext) -> GateResult:
    env = {**os.environ, "PLOWLINE_BASE": ctx.base, "PLOWLINE_HEAD": ctx.head, "PLOWLINE_LANES": " ".join(ctx.lanes)}
    outputs: list[str] = []
    n = len(steps)
    for i, step in enumerate(steps, 1):
        label = f"step {i}/{n} `{step}`"
        if step.startswith("builtin:"):
            code, out = BUILTIN_GATES[step.removeprefix("builtin:").strip()](ctx)
        else:
            try:
                p = subprocess.run(step, shell=True, cwd=ctx.tree, capture_output=True, text=True,
                                   encoding="utf-8", errors="replace", env=env)
            except OSError as e:
                return GateResult("unavailable", f"GATE: UNAVAILABLE at {label}: {e}", "\n".join(outputs))
            code, out = p.returncode, (p.stdout + p.stderr)
        outputs.append(out.strip())
        joined = "\n".join(o for o in outputs if o)
        if code in NOT_FOUND or (os.name == "nt" and code == 1 and NOT_RECOGNIZED in out):
            return GateResult("unavailable", f"GATE: UNAVAILABLE at {label}: exit {code}, the command was not found",
                              joined)
        if code != 0:
            return GateResult("fail", f"GATE: FAIL at {label}, exit {code}", joined)
    return GateResult("pass", f"GATE: PASS ({n} step{'s' if n != 1 else ''})", "\n".join(o for o in outputs if o))
