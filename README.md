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
  to its own worktree with a note. The other lanes still land.
- **No rebases.** The commits that were reviewed are the commits that ship.

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
pl new login-page --paths src/auth/ --brief briefs/login.md --open
pl new rate-limits --paths src/api/limits.py --open
pl watch                # this terminal becomes the control tower
```

Each agent reads its brief (`pl show <lane>`), does the work, commits, and runs
`pl done <lane>`, which sends the lane to review. `pl watch` runs a train whenever
a lane passes: merge onto main, gate once, push once.

If a lane bounces:

```bash
pl resume rate-limits   # prints why, and the merge-main-and-fix steps
```

`pl clean` removes the worktrees and branches of lanes that landed.

**Working alone with a few agent terminals?** See [the solo guide](docs/SOLO.md).

| Command | What it does |
|---|---|
| `pl setup` | Wizard: gate, reviewer, agent, lane rules. `--yes` for agents. |
| `pl new <lane>` | Worktree + branch + claim. `--paths`, `--brief`, `--open`. |
| `pl open <lane>` | New terminal with the agent running in that lane. `--print` to only show the command. |
| `pl show <lane>` | The lane's brief and its latest note. |
| `pl done <lane>` | Review the lane; on GO it waits for the next train. |
| `pl train` | Merge candidates, gate once, push once (or open one PR). |
| `pl watch` | Control tower: live status, and a train whenever a lane is done. |
| `pl status` | Lanes, states, claims and overlaps. |
| `pl resume <lane>` | Reopen a bounced lane with the reason and the fix steps. |
| `pl clean` | Remove worktrees and branches of landed lanes. |
| `pl demo` | Five lanes and two trains in a throwaway repo. `--keep` to look around after. |

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
gate = "python -m pytest -q"     # must pass on the merged train before anything is pushed
review = "builtin:claude"        # see Review below
regenerate = ""                  # optional: rebuild generated files on the merged tree
agent = "claude"                 # what `pl open` starts in a lane: "codex", "gemini", "aider", ...
land = "push"                    # "push" to the trunk, or "pr": open one pull request per train
on_land = ""                     # optional: runs after each landing (deploy, tag, notify)
worktrees = "../{repo}-lanes"    # where lane worktrees live
```

Leave `remote` empty for **local-only mode**: lanes branch from your local trunk,
and the train moves it forward (fast-forwarding your checkout if it's clean).

`pl open` uses Windows Terminal (or a new console window) on Windows, Terminal on
macOS, and a tmux window, gnome-terminal or konsole on Linux. Anywhere else it
prints the command to paste.

Lane state lives in `.git/plowline/`. It is never committed and never pushed.

### Review

`pl done` runs a reviewer before a lane can join a train.

| `review =` | What runs |
|---|---|
| `""` | No review. Every finished lane is a candidate. |
| `"builtin:claude"` | `claude -p` reads the lane's brief and diff and answers GO or NO-GO. |
| `"builtin:codex"` | The same prompt through `codex exec`. |
| any command | Use this for any other agent, or a script. Runs in the lane's worktree. Exit 0 is GO, and its output becomes the note. It gets `PLOWLINE_LANE`, `PLOWLINE_BRANCH`, `PLOWLINE_BASE`, `PLOWLINE_HEAD` and `PLOWLINE_WORKTREE`. |

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

### How a train decides

1. Take every candidate, oldest first.
2. Merge each onto the current trunk in a scratch worktree. A candidate that
   conflicts is bounced with the conflicting paths, and the rest continue.
3. Run `regenerate` if set, then `gate`, on the merged tree.
4. If the gate is red, drop the last-added lane (the only one the evidence
   isolates), bounce it, and run the gate again on the rest.
5. Push once (or, in local-only mode, move the local trunk; in PR mode, open
   the PR). If the trunk moved underneath, nothing is marked landed. Run the
   train again.
6. Run `on_land` if set.

## Why trunk-based, not gitflow?

A `develop` branch is a second merge point, and with a dozen agents that means
twice the drift. Keep one trunk, land through the train, and control releases by
promoting the same tested commit through environments (staging, then
production). Put human approvals on those promotions rather than on another branch.

## Limits

- One train at a time per repository (a lock file enforces it).
- Overlap detection is path-based. It warns, it does not reason about semantics.
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
