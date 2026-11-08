<p align="center"><img src="docs/hero.png" alt="Plowline: many coding agents, one repo, one merge train" width="100%"></p>

Run several coding agents on one repository without them stepping on each other.
Use whichever CLI agent you like: Claude Code, Codex, Gemini CLI, Aider, or
anything else that edits files.

- **A lane per agent.** Each agent works in its own git worktree on its own
  branch, so no two agents ever share a working tree.
- **A queue that knows who touches what.** Each lane claims the files it expects to change.
  Overlaps are flagged, not blocked.
- **A merge train.** Finished lanes are merged together onto the trunk, tested
  once as a whole, and pushed once.
- **Bounce, don't block.** A lane that conflicts or turns the gate red goes back
  to its own worktree with a notice. The other lanes still land.
- **No rebases.** The commits that were reviewed are the commits that ship.

### What 0.3 adds

- **`builtin:mutants`, a mutation test of the change.** Put it last in `gate`:
  the train mutates only the lines its lanes added or changed and bounces a
  lane whose tests pass without noticing a change to its own code. It uses the
  mutation engine you already have for your language (see [Mutants](#mutants)).
- **`pl check`.** The same check on any branch, with no lane and no train.
  `--explain` prints what it would mutate and the exact engine commands.

### What 0.2 adds

- **Train bisection.** A red train is split until the merged set is green.
  Plowline names the lane that turned it red, bounces only that lane with the
  gate output, and lands the rest.
- **Conflict preview.** `git merge-tree` finds a conflicting lane before anything
  is merged, and its notice names the conflicting files. A conflict only in
  generated files (lockfiles and the like) takes the trunk's side and is rebuilt.
- **A GO is bound to a commit.** A lane that moves after its review is reviewed
  again. NO-GO always wins over GO, whatever dashes the reviewer typed.
- **Lane rules.** Scope checks against `--paths`, `--after` dependencies,
  `--priority`, a `max_lanes` cap, and `pl done` refuses an uncommitted worktree.
- **Auto-repair.** A bounced lane gets a notice file, and Plowline can restart its
  agent with that notice, a bounded number of times.
- **Gate lists, briefs, follow-ups, safe clean.** `gate` takes a list of commands.
  `pl new --brief` writes a brief to fill in. Non-blocking review findings
  collect in one file. `pl clean` backs up `.env` files first.

See [CHANGELOG.md](CHANGELOG.md) for the details.

<p align="center"><img src="docs/how-it-works.png" alt="How Plowline works" width="100%"></p>

## Quickstart

Needs Python 3.11+ and git 2.31+. No other dependencies.

```bash
pipx install git+https://github.com/oxgate-inc/plowline
cd your-repo
pl setup                # wizard: gate, reviewer, lane rules for your agents, commit
```

The command is `pl` (or `plow`). Agents run the same setup
unattended: `pl setup --yes --commit --gate "npm test"`.

Start lanes. `--open` launches a new terminal with your agent already in the lane:

```bash
pl new login-page --paths src/auth/ --brief --open    # writes briefs/login-page.md to fill in
pl new rate-limits --paths src/api/limits.py --brief briefs/limits.md --open
pl watch                # this terminal becomes the control tower
```

Each agent reads its brief (`pl show <lane>`), does the work, commits, and runs
`pl done <lane>`, which checks the lane's scope and sends it to review. `pl watch`
runs a train whenever a lane passes: merge onto main, gate once, push once.

If a lane bounces, it gets a notice in `.plowline/notices/<lane>.md`:

```bash
pl resume rate-limits   # prints the notice: why, the files or gate output, what to do
```

`pl clean` removes the worktrees and branches of lanes that landed. It refuses a
worktree with uncommitted changes, copies untracked `.env*` files to
`.plowline/backups/<lane>/` first, and keeps the lane's log.

**Working alone with a few agent terminals?** See [the solo guide](docs/SOLO.md).

| Command | What it does |
|---|---|
| `pl setup` | Wizard: gate, reviewer, agent, lane rules. `--yes` for agents. |
| `pl new <lane>` | Worktree + branch + claim. `--paths`, `--brief [file]`, `--after <lane>`, `--priority N`, `--open`. |
| `pl open <lane>` | New terminal with the agent running in that lane. `--print` to only show the command. |
| `pl show <lane>` | The lane's brief, its overlaps and its latest note. |
| `pl done <lane>` | Check scope, review the lane; on GO it waits for the next train. |
| `pl train` | Merge candidates, gate once, push once (or open one PR). `--full-steam` keeps going until Ctrl-C. |
| `pl watch` | Control tower: live status, and a train whenever a lane is done. |
| `pl status` | Lanes, states, claims, overlaps, dependencies and the follow-up count. |
| `pl resume <lane>` | Reopen a bounced lane and print its notice. |
| `pl clean` | Remove worktrees and branches of landed lanes; back up `.env` files first. |
| `pl check [<ref>]` | Mutation-test the lines a branch changed (the `builtin:mutants` check). `--base <ref>`, `--explain`. |
| `pl demo` | Five lanes, two trains and a `pl check` in a throwaway repo. `--keep` to look around after. |

<p align="center"><img src="docs/train-run.png" alt="A train run: three lanes land, a conflict and a red gate bounce" width="100%"></p>

Watch it end to end in a throwaway repo:

```bash
pl demo
```

## Have an agent set it up

Paste this into your coding agent, in your repo:

```text
Set up Plowline in this repo by following
https://github.com/oxgate-inc/plowline/blob/main/AGENT-SETUP.md
```

The agent looks around first: your test command, your remote, whether main is
protected, which agent CLIs you have. Then it asks you the setup choices in one
message, with a recommendation for each, and runs the setup. Afterwards it gives
you a short tour with `pl demo` and offers to plan your first lanes.

### Let your agent dispatch

Setup also installs a dispatcher skill at `.claude/skills/plowline/SKILL.md`,
and `AGENTS.md` points Codex and other agents to it. In the main checkout, say
"split this into lanes" and the agent plans small lanes that touch different
files, writes their briefs, starts them once you agree, and reports what
landed. It never edits a lane or pushes to main itself.

## Configuration

`plowline.toml` at the repo root:

```toml
[plowline]
trunk = "main"
remote = "origin"
gate = "python -m pytest -q"     # must pass on the merged train; a list of commands works too
review = "builtin:claude"        # see Review below
regenerate = ""                  # optional: rebuild generated files on the merged tree
generated = []                   # files regenerate rebuilds, e.g. ["package-lock.json"]
agent = "claude"                 # what `pl open` starts in a lane: "codex", "gemini", "aider", ...
repair = ""                      # optional: fixes a bounced lane by itself; see Auto-repair
repair_attempts = 2              # automatic repairs per lane before a person decides
land = "push"                    # "push" to the trunk, or "pr": open one pull request per train
on_land = ""                     # optional: runs after each landing (deploy, tag, notify)
strict_paths = false             # true: changing files outside --paths bounces the lane
max_lanes = 0                    # cap on lanes not yet landed; 0 for no cap
worktrees = "../{repo}-lanes"    # where lane worktrees live
env = ""                         # "stanchion" gives each lane its own database and ports; "off" turns it off
```

Leave `remote` empty for **local-only mode**: lanes branch from your local trunk,
and the train moves it forward (fast-forwarding your checkout if it's clean).

`pl open` uses Windows Terminal (or a new console window) on Windows, Terminal on
macOS, and a tmux window, gnome-terminal or konsole on Linux. Anywhere else it
prints the command to paste.

Lane state lives in `.git/plowline/`. The files people and agents read live in
`.plowline/` at the repo root, which setup adds to `.gitignore`: `notices/`,
`followups.md`, `logs/` and `backups/`. Neither is ever committed or pushed.

### Gate lists

`gate` is one command or a list. The commands run in order on the merged tree and
the first red one stops the run:

```toml
gate = ["npm run lint", "npm run typecheck", "npm test"]
```

Every gate run ends with one line: `GATE: PASS`, `GATE: FAIL at step 2/3 ...` or
`GATE: UNAVAILABLE`. The verdict comes from the exit codes Plowline captured
itself. A command that could not start at all (not found) is UNAVAILABLE, not
red: a red result is not proof the check ran, so the train blames no lane, stops,
and leaves the candidates as they were. Each step gets `PLOWLINE_BASE`,
`PLOWLINE_HEAD` and `PLOWLINE_LANES`. A step written `builtin:<name>` runs a
check that ships with Plowline instead of a command. There is one so far,
`builtin:mutants`.

### Mutants

A test suite can pass without testing the code it covers: a reversed condition
survives when every test happens to use a case where both branches agree.
`builtin:mutants` changes the lines the train added or changed on purpose
(`<` to `<=`, `not x` to `x`, a call removed), runs your tests against each
change, and reports every change no test noticed. More survivors than
`max_survivors` (default 0) turn the gate red, and bisection bounces the lane
whose lines they are on, with each survivor in its notice.

```toml
gate = ["npm run lint", "npm test", "builtin:mutants"]
```

Put it last: it is the slowest step, and it only means something once the tests
pass. Each survivor is one line in the gate output and the bounce notice:

```text
MUTANTS: FAIL (1 survivor on 1 changed line)
tasks/counter.py:3  removed not  (no test failed)
```

The mutating is done by the engine for each language, which you install. Files
are matched by extension:

| Engine | Languages | Install | How it is scoped |
|---|---|---|---|
| [mutmut](https://mutmut.readthedocs.io/) 3 | Python | `pip install mutmut` (Linux, macOS, or WSL on Windows) | runs only the changed functions' mutants (`mutmut run "<module>.x_<function>__mutmut_*"`); Plowline keeps the ones on changed lines |
| [StrykerJS](https://stryker-mutator.io/docs/stryker-js/introduction/) | JavaScript, TypeScript | `npm install --save-dev @stryker-mutator/core` and your test runner's plugin | `--mutate "src/x.ts:5-7,..."`: the changed line ranges |
| [Stryker.NET](https://stryker-mutator.io/docs/stryker-net/introduction/) | C# | `dotnet tool install dotnet-stryker` | `--since:<base>`: the changed files; Plowline keeps the survivors on changed lines |
| [cargo-mutants](https://mutants.rs/) | Rust | `cargo install --locked cargo-mutants` | `--in-diff <the train's diff>`; Plowline reads `outcomes.json` |
| [Gremlins](https://gremlins.dev/) | Go | `go install github.com/go-gremlins/gremlins/cmd/gremlins@latest` | `--diff <base>`; Plowline keeps the survivors on changed lines |

Each engine reads its own configuration as usual (`[tool.mutmut]` in
`pyproject.toml`, `stryker.config.json`, `stryker-config.json`,
`.cargo/mutants.toml`, `.gremlins.yaml`). Test files (`test_*.py`, `tests.py`,
`*_tests.py`, `conftest.py`, `*_test.*`, `*.test.*`, `*.spec.*`, `*.cy.*`,
`*Tests.cs`, `*Test.cs`, `*Spec.cs`, anything under a `tests/`, `test/`,
`__tests__/`, `e2e/` or `*.Tests/` folder), `generated` files and `skip` globs
are never mutated; add other test layouts to `skip`.

A mutant counts against a lane when the lines it changed overlap the lane's
changed lines: its whole span where the engine reports one (a "replace this
function's body" mutant covers the body), its line where the engine gives only
a line. A changed file whose changed lines hold code, but on which the engine
judged no mutant, is UNAVAILABLE, never a pass: the report left the file out
(a path or module name did not match), or the engine skipped, ignored or could
not compile every mutant there. An edit only to comments, blank lines, imports
or brackets has nothing to mutate; it passes, named in the report.

For any other language, or to replace a built-in engine, name a command for the
extension:

```toml
[mutants]
max_survivors = 0     # survivors on changed lines a train may keep
budget = 600          # seconds for all engines in one run
skip = ["src/legacy/**", "*.pb.go"]   # never mutated; same globs as --paths

[mutants.engines.kt]
command = "python tools/mutate_kotlin.py {ranges}"
```

The command runs at the root of the merged tree with `{files}` (the changed
files), `{ranges}` (`file:a-b` for each changed range), `{base}`, `{head}` and
`{diff}` (a file holding the train's diff) filled in; write them bare, they
arrive quoted. The contract:

- exit 0: no survivors;
- exit 1: survivors, and the command prints one line per survivor,
  `path:line what was mutated` (`path` relative to the repository root). Only
  lines on changed lines count;
- exit 1 with no such line, any other exit code, or a command that is not
  found: UNAVAILABLE. A crash is never read as survivors.

`pl check` runs the same check in your worktree, with no lane and no train: on
`HEAD` (or `pl check <ref>`), against the merge-base with the trunk (or
`--base <ref>`). It exits 0 on PASS, 1 on FAIL and 2 when it could not run.
It refuses a worktree with uncommitted changes, because the engines would test
code the line numbers don't describe; `pl check --dirty` checks the working
tree instead, diffing it (tracked files) against the base. New files git does
not track yet are not in that diff: they are listed as "not checked" until you
`git add` them. `pl check --explain`
prints the files and line ranges it would mutate and the exact engine commands,
and runs nothing. On the trunk itself it says `nothing to mutate`. When the
gate has `builtin:mutants`, `pl new --brief` asks the lane's agent to run
`pl check` before `pl done`.

A run leaves the tree as it found it: an engine's output path that was already
there (`mutants/`, `.stryker-tmp/`, `reports/mutation/mutation.json`,
`StrykerOutput/`) is renamed to `<name>.plowline-aside` for the run and put
back afterwards, and what the engine wrote there is removed. Any other tracked
file that changes during the run is left exactly as it is and named in the
report: a person or an editor may have saved it. Anything Plowline could not
undo is named at the end of the report.

If Plowline itself is killed mid-run (the terminal closed, the process ended),
it cannot clean up. You may find `<name>.plowline-aside` next to where `<name>`
was: it is your own earlier folder or file. Delete what the engine left at
`<name>`, rename the aside back, and restore a tracked file with
`git checkout -- <path>`. Until then the next run stops with UNAVAILABLE and
names the aside, so nothing is overwritten.

Limits:

- The engines are external. A missing engine is UNAVAILABLE: the train stops,
  blames no lane, and leaves the candidates as they were.
- The scope is the diff. Code a lane did not touch is not mutated again, even
  when the lane weakened the tests that cover it.
- The `budget` covers all engines in one run, and every train, and each half of
  a bisection, runs it again. Over budget, the engine and everything it started
  are stopped and the step is UNAVAILABLE: a timeout by itself never passes the
  train and never bounces a lane. The one exception: survivors an earlier
  engine already proved in the same run still make it FAIL. Raise `budget` or
  narrow it with `skip`.
- Only languages with an engine are checked. A change only to other files
  (`.java`, `.rb`, `.kt` with no `[mutants.engines]` entry), or only to tests,
  passes as `MUTANTS: nothing to mutate`, with the files left out counted on
  that line; in a run that does mutate something, they are listed under
  "not mutated".
- mutmut mutates function bodies only: a change only to module-level Python
  code (constants, top-level statements, class attributes) has nothing to
  mutate and passes with a note. It does not run on Windows outside WSL. For
  Python, the killed count is per changed function, not per line. A survivor
  whose code appears more than once in its function is placed on the
  function's first line.
- Gremlins reports file names relative to the Go module. When two changed files
  end in the same name (`a/pkg/x.go`, `b/pkg/x.go`), the mutant cannot be
  placed and the step is UNAVAILABLE.
- A production package that is literally named `test`, `tests` or `e2e` is
  treated as tests and never mutated.

### Review

`pl done` runs a reviewer before a lane can join a train.

| `review =` | What runs |
|---|---|
| `""` | No review. Every finished lane is a candidate. |
| `"builtin:claude"` | `claude -p` reads the lane's brief and diff and answers GO or NO-GO. A refusal in the first lines wins, however it is written (`NO-GO`, `NO GO`, `No go`, `not GO`, any dash), and an answer with neither is a NO-GO. |
| `"builtin:codex"` | The same prompt through `codex exec`. |
| any command | Use this for any other agent, or a script. Runs in the lane's worktree. Exit 0 is GO unless its first lines say NO-GO, and its output becomes the note. It gets `PLOWLINE_LANE`, `PLOWLINE_BRANCH`, `PLOWLINE_BASE`, `PLOWLINE_HEAD` and `PLOWLINE_WORKTREE`. |

The reviewer is a gate, not a substitute for your tests. Keep the real checks in `gate`.

### Landing

| `land =` | What the train does with a green batch |
|---|---|
| `"push"` | Pushes it to the trunk (or moves the local trunk in local-only mode). |
| `"pr"` | Pushes it to a `plowline/train-<time>-<commit>` branch and opens one pull request with `gh`. The lanes wait as `proposed`, and the next train waits for that PR. When it merges, by any method, the lanes are marked landed. If it is closed unmerged, they bounce. Use it when the trunk is protected or needs a human approval. |

`on_land` runs in the repo root after each landing, once per train. It gets
`PLOWLINE_TRUNK`, `PLOWLINE_COMMIT`, `PLOWLINE_LANES`,
`PLOWLINE_BRANCHES` and, in PR mode, `PLOWLINE_PR`. Use it to deploy, tag,
or post a message. If it fails, the train reports it and the lanes stay landed.

```toml
on_land = "./scripts/deploy-staging.sh"
```

### Lane environments

Worktrees separate the code, not the runtime: two lanes still share your dev
server's port and your database. [Stanchion](https://github.com/oxgate-inc/stanchion)
gives each lane its own block of ports, its own copy of your database and an
env file in its worktree. Install it and turn it on:

```toml
[plowline]
env = "stanchion"
```

`pl new` then runs `stanchion up <lane> --path <worktree>`, `pl resume` runs it
again, and a lane that lands or is bounced gets `stanchion down <lane>`. A bounce
drops the lane's database, so a resumed lane starts from a fresh copy of the
template. Stanchion's own `stanchion.toml` says which database to copy and which
variables to write.

A `[stanchion]` table in `plowline.toml` also turns it on, and `env = "off"` turns
it off even when the table is there:

```toml
[stanchion]
command = "stanchion"   # how to run it; on Windows a path such as 'C:\Tools\stanchion.exe' works
timeout = 300           # seconds Plowline waits for each up or down
```

If Stanchion is not on PATH, Plowline prints one line and the lane opens anyway.
A failed or timed-out `up` or `down` is printed and the lane carries on. A failed
`down` leaves that lane's database and ports behind, and `pl clean` does not retry
it: run `stanchion down <lane>`, or `stanchion gc` once `pl clean` has removed the
worktree.

### How a train decides

1. Take every candidate, highest `--priority` first, then oldest first. Skip a
   lane whose `--after` lane has not landed yet, and a lane whose branch moved
   after its GO (it goes back to `active` to be reviewed again).
2. Preview each merge onto the current trunk with `git merge-tree` (git 2.38+;
   older git tries the merge itself). A candidate that conflicts is bounced with
   the conflicting paths, and the rest continue. A conflict only in `generated`
   files takes the trunk's side, when `regenerate` is set to rebuild them.
3. Run `regenerate` if set, then `gate`, on the merged tree.
4. If the gate is red, bisect (below).
5. Push once (or, in local-only mode, move the local trunk; in PR mode, open
   the PR). If the trunk moved underneath, nothing is marked landed. Run the
   train again.
6. Run `on_land` if set.

### Full steam

Hitch the ox and walk away: `pl train --full-steam` (or `--watch`) checks every
30 seconds (`--every N`) and runs one train whenever lanes are done, with
everything above (preview, gate, bisection, landing, notices, auto-repair) and
`max_lanes` and `repair_attempts` as configured. It prints one line per train,
`14:02:11  2 landed, 1 bounced (lane/b: gate red), 3 working, next check in 30s`,
stays quiet when idle except for a heartbeat every ten minutes, and sends the
train's own output to `.plowline/logs/train.log`. A lock in `.plowline/` keeps two
trains from running at once (a lock older than `--cycle-timeout` is reported and
removed), a cycle that crashes is logged and the loop carries on until five fail
in a row, `--until-empty` stops it once no lane is working, done or bounced, and
Ctrl-C mid-train lets the train finish its landing or rollback before the loop
stops, so the trunk is never left half-landed.

### Train bisection

A red gate on five merged lanes says one of them broke it, not which. Plowline
splits the train and gates the halves, keeping every half that is green, until
it finds each lane that turns a green set red. Those lanes bounce with the gate
output; every other lane lands in the same train. With two lanes, the first
probe is "drop the last one". A lane is only blamed when the gate goes red with
it on top of a set already proven green, and if the trunk alone is red the train
stops with `GATE_RED_ON_TRUNK` and blames nobody. One red lane among n costs
about log2(n) extra gate runs.

Two lanes that are each green but red together cannot both be right: the one
later in train order is blamed (it went red on top of a set proven green), and
the earlier one lands. A lane that merged fine with the whole train but no
longer merges once the red lanes are left out bounces as a merge conflict, with
the paths, not as a red gate.

### Lane rules

- **Scope.** `pl done` diffs the lane against its merge-base with the trunk (so a
  lane that merged the trunk is not blamed for the trunk's files) and lists every
  changed file outside `--paths`. That is a warning; with `strict_paths = true` it
  bounces the lane. Files in `generated` never count. Overlapping claims between
  lanes are only ever a warning, shown by `pl new`, `pl show` and `pl status`.
- **Dependencies.** `pl new api-client --after api` makes `api-client` wait: it
  can be reviewed, but it joins a train only once `api` has landed.
- **Order and cap.** `--priority N` (default 0, higher first) orders the train.
  `max_lanes = 4` caps the lanes not yet landed.
- **Refusals and notes.** `pl new` refuses a branch or worktree that already
  exists and a full cap, each with a one-line reason. In local-only mode it also
  refuses while `main` has uncommitted changes (the lane would start without
  them); with a remote it just says what the lane will not include: uncommitted
  changes, or commits not pushed yet. A lane name can be used again once the old
  lane is cleaned: its log and notice are kept as `<lane>.1`. `pl done` refuses a worktree with
  uncommitted changes and prints exactly what is uncommitted.
- **A GO is bound to a commit.** `pl done` records the lane head and the trunk it
  was reviewed on. The train merges exactly that head; if the branch moved, the
  GO is stale and the lane is reviewed again. A moved trunk is fine: the train
  gates the merged result anyway.
- **Parked lanes.** A lane whose worktree was deleted shows as `parked` in
  `pl status`, and `pl done` prints the command that recreates it.

### Auto-repair

Every bounce writes `.plowline/notices/<lane>.md`: the reason, the conflicting
paths or the files outside the claim, the last 40 lines of gate output, and what
to do (merge the trunk, never rebase, fix, re-test, `pl done`). `pl resume <lane>`
prints it.

To have bounces fix themselves, set `repair` to a command that runs your agent
without a person in the loop. When a train bounces a lane, Plowline starts it in
the lane's worktree with the notice on stdin (long text never goes in the command
line); `{lane}`, `{path}` (the worktree) and `{notice}` (the notice file) are
filled in if the command uses them. Its output goes to
`.plowline/logs/<lane>.log`. The agent fixes the lane and runs `pl done` itself.

```toml
repair = "./scripts/fix-lane.sh {lane} {path}"
repair_attempts = 2
```

Write placeholders bare (`{path}`, not `"{path}"`): Plowline fills them in
already quoted for your system.

After `repair_attempts` restarts, the next bounce prints "split this lane or
re-plan" and Plowline stops restarting it. The count starts over when the lane
lands, or when a person runs `pl done` on it; the repair command runs with
`PLOWLINE_REPAIR` set, so its own `pl done` keeps the count. Leave `repair` empty and bounces wait
for you: open the lane (`pl open <lane>`) and tell the agent to run
`pl resume <lane>`.

### Briefs, reports and follow-ups

`pl new <lane> --brief` writes `briefs/<lane>.md` with five sections: Goal, Files
you may touch, Do not touch, Acceptance, Report. `pl show` and the reviewer read
it live, so you can keep editing it until the agent starts. The lane prompt, the
brief and the review prompt all ask for the same closing block:

```text
COMMIT: <sha>
COMMANDS RUN: <each command, with its exit code>
RESULT: <what passed or failed, with counts>
NOT VERIFIED: <what was not checked>
```

Reviewers put findings that should not block a lane under `## Follow-ups`. On GO,
`pl done` appends them to `.plowline/followups.md` and `pl status` shows the
count, so the small stuff becomes the next lanes instead of getting lost.

## Why trunk-based, not gitflow?

A `develop` branch is a second merge point, and with a dozen agents that means
twice the drift. Keep one trunk, land through the train, and control releases by
promoting the same tested commit through environments (staging, then
production). Put human approvals on those promotions rather than on another branch.

## Limits

- One train at a time per repository (a lock file enforces it).
- Overlap and scope checks are path-based. They do not reason about semantics.
- Bisection assumes the gate is deterministic. A flaky test can get the wrong
  lane bounced; its notice carries the gate output to check.
- Local-only mode refuses to land while your trunk checkout has uncommitted
  changes to tracked files. Commit or stash them, and the next train lands.
- PR mode keeps one train PR open at a time, and needs the GitHub CLI.

## What it is not

Plowline is a local tool. There is no server, no account, no telemetry and no
dashboard. It talks to git, your gate, your reviewer CLI, and `gh` if you choose
PR mode. Everything it knows is in `.git/plowline/` on your machine.

## License

Apache-2.0. See [LICENSE](LICENSE).

---

<sub><img src="docs/ox-mark.svg" height="14" alt=""> Built by <a href="https://oxgate.ai">Oxgate, Inc.</a></sub>
