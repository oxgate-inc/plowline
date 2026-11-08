# Changelog

## 0.3.0 (2026-11-08)

- **`builtin:mutants`.** The first built-in gate step: a mutation test of the
  lines the train added or changed. Scope comes from `git diff -U0 base..head`;
  test files, `generated` files, `[mutants] skip` globs and extensions with no
  engine are left out, and a change with nothing left passes with
  `MUTANTS: nothing to mutate`. Engines by extension: mutmut 3 (`.py`),
  StrykerJS (`.js .jsx .ts .tsx .mjs .cjs`), Stryker.NET (`.cs`), cargo-mutants
  (`.rs`) and Gremlins (`.go`); `[mutants.engines.<ext>] command = "..."` adds
  or replaces one, with `{files}`, `{ranges}`, `{base}`, `{head}` and `{diff}`.
  Survivors on changed lines above `max_survivors` (default 0) are a FAIL, and
  the report lists them as `path:line  what was mutated  (no test failed)`.
  A missing engine, an engine that could not report, or a run over `budget`
  (default 600 s, all engines together; the engine's process tree is stopped)
  is UNAVAILABLE: the train stops and blames no lane.
- **Built-in steps can say UNAVAILABLE.** A `builtin:` step returns pass, fail
  or unavailable; unavailable ends the gate with `GATE: UNAVAILABLE at step
  i/n`, like a command that was not found. A built-in step that crashes is
  UNAVAILABLE too. Shell steps are unchanged.
- **Never a quiet pass, never a crash read as red.** A changed file the
  engine's report does not cover, a mutant status Plowline cannot judge, an
  engine report it cannot read, or a generic command that exits 1 without
  listing survivors is UNAVAILABLE. A mutant counts when its span overlaps a
  changed line (cargo-mutants spans come from `outcomes.json`). An engine's
  earlier output is moved aside for the run and put back, and tracked files it
  changed are checked out again.
- **`pl check [<ref>] [--base <ref>] [--explain] [--dirty]`.** The same check in
  your worktree, with no lane and no train: `HEAD` against its merge-base with
  the trunk by default. Exit 0 pass, 1 fail, 2 unavailable (a crash too). A ref
  other than `HEAD` is checked in a temporary worktree. Uncommitted changes are
  refused unless `--dirty`, which checks the working tree against the base.
  `--explain` prints the files, line ranges and engine commands, and runs
  nothing.
- **Briefs.** With `builtin:mutants` in the gate, `pl new --brief` asks the
  agent to run `pl check` before `pl done`.
- **Setup and example config** carry a commented `[mutants]` block.
- **`pl demo`** adds lane f: a reversed condition whose test passes anyway.
  `pl check` names the line when mutmut is installed, and says how to get it
  when not.

## 0.2.0 (2026-11-07)

### Train

- **Bisection on a red gate.** A red train is split until the merged set is
  green. Each lane that turns a green set red is bounced with the gate output;
  the rest land in the same train. Of two lanes red only together, the later one
  is blamed. A lane that stops merging once red lanes are dropped bounces as a
  merge conflict. With two lanes the first probe is still "drop
  the last one". A red trunk (`GATE_RED_ON_TRUNK`) blames no lane.
- **Conflict preview.** With git 2.38+, `git merge-tree --write-tree --name-only`
  finds a conflicting lane without touching the scratch tree; older git falls
  back to trying the merge. The notice names the conflicting paths.
- **Generated-file conflicts.** `generated = ["package-lock.json", ...]`: when
  every conflicting path is generated and `regenerate` is set, the train takes
  the trunk's side and regenerates. Any other conflict bounces as before.
- **A GO is bound to its head.** `pl done` records the lane head and the trunk
  commit it was reviewed on. A lane whose branch moved after its GO goes back to
  `active` (`STALE_REVIEW`) instead of riding an old verdict. A moved trunk is
  reported, and the train gates the merged result as it always did.
- **Verdict parsing.** A refusal is read before GO in the reviewer's first eight
  lines, after folding Unicode dashes (U+2010..U+2015, U+2212, U+FF0D) to `-`:
  `NO-GO`, `NO GO`, `No go`, `NOGO` and `not GO` are all NO-GO. A
  command reviewer that exits 0 but says NO-GO is a NO-GO. A built-in reviewer
  that names neither is a NO-GO.
- **Gate lists.** `gate` takes a list of commands, run in order, stopping at the
  first red. Every run ends with `GATE: PASS`, `GATE: FAIL ...` or
  `GATE: UNAVAILABLE`; a gate command that cannot start blames no lane. Steps get
  `PLOWLINE_BASE`, `PLOWLINE_HEAD` and `PLOWLINE_LANES`. `builtin:<name>` steps
  are reserved for checks that ship with Plowline (`builtin:mutants` in 0.3).

- **`pl train --full-steam`** (alias `--watch`): a train whenever lanes are
  done, checked every `--every N` seconds (default 30), one line per cycle, a
  heartbeat every ten minutes when idle, `--until-empty` to stop when no lane is
  left in play. Crashed cycles go to `.plowline/logs/train.log`; five in a row
  stop the loop. Ctrl-C during a cycle finishes that cycle first. The train lock
  moved to `.plowline/train.lock`, and full steam removes a lock older than
  `--cycle-timeout`.

### Lanes

- **Scope check at `pl done`.** Changed files are measured from the merge-base
  with the trunk and compared to `--paths`. Files outside the claim are a
  warning, or a bounce with `strict_paths = true`. Overlaps stay advisory and
  now show in `pl show`.
- **`--after <lane>`.** A lane joins a train only once the named lane has landed.
- **`--priority N` and `max_lanes`.** Higher priority merges first. `pl new`
  refuses an existing branch or worktree, a full cap, or (local-only mode) a
  trunk checkout with uncommitted changes, each with a one-line reason. With a
  remote it notes uncommitted or unpushed trunk work the lane will start
  without. Reusing the name of a cleaned lane keeps the old log and notice as
  `<lane>.1`.
- **Dirty worktrees.** `pl done` prints exactly what is uncommitted and stops.
- **Notices and auto-repair.** Every bounce writes `.plowline/notices/<lane>.md`
  (reason, paths or gate output, what to do); `pl resume` prints it. With
  `repair = "<command>"` (placeholders `{lane}`, `{path}`, `{notice}`), a train
  bounce starts that command in the lane with the notice on stdin, up to
  `repair_attempts` (default 2), then prints "split this lane or re-plan". The
  count starts over when the lane lands or a person runs `pl done`.
  `agent` keeps meaning what `pl open` starts.
- **Brief scaffold.** `pl new <lane> --brief` writes `briefs/<lane>.md` with Goal,
  Files you may touch, Do not touch, Acceptance and Report. Briefs are read live.
  The lane prompt, the brief and the review prompt ask for a closing block:
  COMMIT, COMMANDS RUN, RESULT, NOT VERIFIED.
- **Follow-ups.** Review findings under `## Follow-ups` are appended to
  `.plowline/followups.md` on GO; `pl status` shows the count.
- **Safe `pl clean`.** Refuses a dirty worktree, backs up untracked `.env*` files
  to `.plowline/backups/<lane>/`, and keeps the lane's log in
  `.plowline/logs/`. A lane whose worktree is missing shows as `parked`.
- `pl setup` adds `.plowline/` to `.gitignore` and writes the new keys with their
  defaults.

## 0.1.0

First release: lanes in worktrees, path claims and overlap warnings, review
before a lane joins a train, a merge train that gates once and pushes once,
PR mode, `on_land`, `pl watch`, `pl demo`, and the Stanchion hook for lane
environments.
