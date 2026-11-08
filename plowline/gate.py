"""The gate: the commands that must pass on the merged train before anything lands.

`gate` is one command or a list of commands. They run in order on the merged tree and the first red one stops
the run. A step written `builtin:<name>` runs a check that ships with Plowline instead of a shell command:
`builtin:mutants` is a mutation test of the lines the train changed (see mutants.py).

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
_ISOLATED = [False]


def isolate_children(on: bool) -> None:
    """Full steam: run gates, regenerate and on_land in their own process group, so a Ctrl-C at the terminal
    reaches Plowline alone and the running cycle finishes, instead of a killed gate reading as a red one.
    Plain `pl train` leaves it off, so Ctrl-C stops the gate's whole process tree as it always did."""
    _ISOLATED[0] = on


def child_group() -> dict:
    if not _ISOLATED[0]:
        return {}
    return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt" else {"start_new_session": True}


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


@dataclass
class StepResult:
    """What a built-in step returns: pass, fail or unavailable (it could not judge), its full output, and the one
    line the GATE: line quotes."""
    status: str
    output: str
    summary: str = ""


def _mutants(ctx: GateContext) -> StepResult:
    from plowline import mutants  # imported on use: mutants imports this module
    return mutants.gate_step(ctx)


# name -> check(ctx) -> StepResult, or (exit code, output). Registered by Plowline itself, never from configuration.
BUILTIN_GATES: dict[str, Callable[[GateContext], StepResult | tuple[int, str]]] = {"mutants": _mutants}


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
                known = ", ".join(f"builtin:{n}" for n in sorted(BUILTIN_GATES))
                raise GateConfigError(f"gate step {step!r}: no built-in gate named {name!r} (built-in steps: {known})")
    return steps


def run_gate(steps: list[str], ctx: GateContext) -> GateResult:
    env = {**os.environ, "PLOWLINE_BASE": ctx.base, "PLOWLINE_HEAD": ctx.head, "PLOWLINE_LANES": " ".join(ctx.lanes)}
    outputs: list[str] = []
    n = len(steps)
    for i, step in enumerate(steps, 1):
        label = f"step {i}/{n} `{step}`"
        if step.startswith("builtin:"):
            try:
                ran = BUILTIN_GATES[step.removeprefix("builtin:").strip()](ctx)
            except Exception as e:  # a crash in Plowline's own check is not proof against any lane
                return GateResult("unavailable", f"GATE: UNAVAILABLE at {label}: it crashed: {type(e).__name__}: {e}",
                                  "\n".join(o for o in outputs if o))
            if isinstance(ran, StepResult):
                outputs.append(ran.output.strip())
                joined = "\n".join(o for o in outputs if o)
                summary = ran.summary or (ran.output.strip().splitlines() or [ran.status])[0]
                if ran.status not in ("pass", "fail"):  # unavailable, or a status Plowline does not know
                    why = summary if ran.status == "unavailable" else f"unknown status {ran.status!r}: {summary}"
                    return GateResult("unavailable", f"GATE: UNAVAILABLE at {label}: {why}", joined)
                if ran.status == "fail":
                    return GateResult("fail", f"GATE: FAIL at {label}: {summary}", joined)
                continue
            code, out = ran
        else:
            try:
                p = subprocess.run(step, shell=True, cwd=ctx.tree, capture_output=True, text=True,
                                   encoding="utf-8", errors="replace", env=env, **child_group())
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
