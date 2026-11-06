# Solo guide: one person, several agent terminals

You have one repo and want three coding agents working on it at once,
without them overwriting each other or leaving you a pile of branches to merge.
This guide gets you there in about ten minutes.

- [What you end up with](#what-you-end-up-with)
- [Set up once](#set-up-once)
- [A working session](#a-working-session)
- [When a lane bounces](#when-a-lane-bounces)
- [Habits that keep it smooth](#habits-that-keep-it-smooth)
- [Troubleshooting](#troubleshooting)

## What you end up with

```
┌──────────────────────────┐  ┌───────────────┐  ┌───────────────┐  ┌───────────────┐
│ control tower            │  │ lane: login   │  │ lane: limits  │  │ lane: docs    │
│ your repo · pl watch     │  │ your agent    │  │ your agent    │  │ your agent    │
│ status + automatic trains│  │ own worktree  │  │ own worktree  │  │ own worktree  │
└──────────────────────────┘  └───────────────┘  └───────────────┘  └───────────────┘
```

- **One control-tower terminal** in your repo, running `pl watch`. You don't
  edit code here.
- **One terminal per lane.** Each runs your agent in its own worktree
  (`../<repo>-lanes/<lane>`) on its own branch (`lane/<lane>`).
- Each agent finishes by running `pl done <lane>`. A reviewer checks the diff,
  and `pl watch` lands every lane that passes with one merge, one test run and one push.

## Set up once

Needs Python 3.11+, git 2.31+ and your CLI agent of choice (Claude Code, Codex,
Gemini CLI, Aider, ...) on your PATH.

```bash
pipx install git+https://github.com/oxgate-inc/plowline
cd ~/code/your-repo
pl setup
```

The wizard asks six things. The defaults are usually right:

| Question | What to answer |
|---|---|
| Remote | `origin` if the repo is on GitHub. `-` for **local-only**: no remote, and the train moves your local `main`. |
| Trunk | Usually `main`. |
| Gate | Your full test command, e.g. `npm test` or `python -m pytest -q`. It must pass now. |
| Reviewer | `builtin:claude` or `builtin:codex`, a command that runs your agent on the diff, or `-` for none. |
| Agent | The command that starts your agent, e.g. `claude`, `codex`, `gemini` or `aider`, with a cheaper model flag if you like. |
| Regenerate | Leave empty unless most changes touch a generated file (lockfile, generated docs). |

Say yes to commit, and yes to push if you have a remote. The setup adds the lane
rules to `AGENTS.md` and `CLAUDE.md`, so the agent in each lane knows how to behave.

## A working session

**1. Pick 2–4 lanes.** Good lanes are small (an hour or two of agent work) and
touch different files. Write a short brief for each by copying `briefs/TEMPLATE.md`:
the goal, the files, and how you'll know it's done.

**2. Start them.** Each command creates the worktree and opens a new terminal
with your agent already told to read its brief:

```bash
pl new login  --paths src/auth/        --brief briefs/login.md  --open
pl new limits --paths src/api/limits.py --brief briefs/limits.md --open
pl new docs   --paths docs/             --brief briefs/docs.md   --open
```

If two lanes claim the same files you'll see an overlap warning. That's
allowed, but expect one of them to bounce and need a merge.

**3. Turn the current terminal into the control tower:**

```bash
pl watch
```

It refreshes every 15 seconds. When an agent runs `pl done <lane>` and the
reviewer says GO, the next refresh runs a train:

```
landed 2 lane(s) on origin/main at 4c94787
```

You can also hand step 1 and 2 to your agent: in the control-tower checkout, before
`pl watch`, say "split this into lanes: …". The dispatcher skill that setup
installed has it propose the lanes, write the briefs, and start them once you agree.

**4. Talk to your agents as usual.** Each lane terminal is a normal agent
session. Answer its questions, steer it, approve its tools. You never need to
`cd`, merge or push by hand.

**5. Finish up.** When everything has landed:

```bash
pl clean      # removes landed worktrees and branches
```

## When a lane bounces

A lane bounces when it conflicts with something that landed first, or when the
combined tests go red. The other lanes still land. `pl watch` shows the reason.
Go to that lane's terminal and tell the agent:

> Your lane bounced. Run `pl resume <lane>` and fix it.

The agent merges `main` into its lane (never a rebase), fixes the cause,
re-runs the tests, commits and runs `pl done <lane>` again. The next train picks it up.

A NO-GO from the reviewer isn't a bounce. The agent sees the findings straight
away in its own terminal and fixes them.

## Habits that keep it smooth

- **Don't edit in the control-tower checkout.** In local-only mode the train
  won't land while your trunk checkout has uncommitted changes.
- **Claim honestly.** `--paths` is how Plowline warns you about overlaps
  before they become conflicts.
- **Small lanes land.** One feature or fix per lane. Big lanes conflict more and
  are harder to review.
- **Three is the sweet spot** for one person. You're still the one answering questions.
- **Watch your usage.** Every lane is an agent session, and every `pl done` is
  one more agent call for the review.

## Troubleshooting

| You see | Do |
|---|---|
| `pl open` printed a command instead of opening a terminal | No supported terminal was found. Paste the command into a new terminal. |
| `lane ... has uncommitted changes; commit them first` | The agent stopped before committing. Ask it to commit, then run `pl done` again. |
| `push refused (did origin/main move?)` | Someone pushed to main during the train. `pl watch` retries on its next refresh. |
| `...has uncommitted changes on main` (local-only) | Commit or stash in the control-tower checkout. |
| `another train holds .../train.lock` | Another `pl train` or `pl watch` is running. If none is, delete the lock file. |
| A lane is stuck as `active` | The agent hasn't run `pl done`. Check its terminal. |
