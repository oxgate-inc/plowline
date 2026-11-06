# Setting up Plowline (a script for a coding agent)

You are helping a person set up Plowline in their repository, so that several
coding agents can work on it at once. Walk them through it in four parts:
look, ask, set up, teach. Talk plainly. Recommend a default for every choice,
and explain any term the first time you use it.

## Part 1: Look before you ask

Find out as much as you can yourself, so the person only answers real choices.

```bash
git --version          # need 2.31 or newer
python --version       # need 3.11 or newer (python3 on some systems)
git status             # should be clean, on the main branch
git remote -v          # is there a remote? usually origin
which claude codex gemini aider gh  # which agent CLIs and the GitHub CLI are installed
```

- If the working tree is dirty, stop and ask what to do with the changes.
- Find the test command CI runs: look in `.github/workflows/`, `Makefile`,
  `package.json`, `pyproject.toml`. Run it once. If it fails before anything
  else happens, stop and tell the person: the train can't work on a red trunk.
- If `gh` is installed and the remote is on GitHub, check whether the main
  branch is protected: `gh api repos/{owner}/{repo}/branches/main/protection`.
  A 404 means it isn't.
- Note any generated files that most changes touch (lockfiles, generated
  docs, schema snapshots) and the command that rebuilds them.

## Part 2: Ask once, with your recommendations

Send the person ONE message with a short explanation and a numbered list. Put
your recommendation and the reason next to each item, so they can answer
"all your defaults" or change just one. Skip items whose answer you already
know for certain.

> Plowline gives every agent its own copy of the repo (a *lane*). When a
> lane is done, a reviewer checks it, and a *train* merges the finished lanes
> onto main together, runs your tests once, and lands them. A lane that
> conflicts or breaks the tests goes back to its agent; the others still land.
>
> 1. **Where lanes land.** `push` straight to main (recommended unless main is
>    protected), `pr` to open one pull request per train for you to approve, or
>    **local-only** if there's no remote.
> 2. **The gate**, the command that must pass before anything lands. I found
>    `<command>` and it passes now.
> 3. **Reviewer.** `builtin:claude` or `builtin:codex` if they use one of
>    those, otherwise a command that runs their agent on the diff, or none.
>    Each review is one extra agent call per lane.
> 4. **Lane agent**, the command that starts an agent in a new lane: the CLI
>    agent they already use (`claude`, `codex`, `gemini`, `aider`, ...), with a
>    cheaper model flag if they want cheaper lanes.
> 5. **After each landing**, an optional command, such as a staging deploy or a
>    chat message. Recommended: none for now.
> 6. **Generated files.** Only if I found some: `<command>` rebuilds them after
>    merging, so lanes don't conflict on them.

Wait for the answers. If an answer is unclear, ask about that item only.

## Part 3: Set up

Install, then run setup unattended with their answers:

```bash
pipx install git+https://github.com/oxgate-inc/plowline
#   no pipx: python -m pip install git+https://github.com/oxgate-inc/plowline

pl setup --yes --commit \
  --gate "<gate>" \
  --review "<builtin:claude | builtin:codex | command | ''>" \
  --agent "<lane agent>" \
  --land <push | pr> \
  [--remote ""]                 # local-only
  [--on-land "<command>"]       # only if they asked
  [--regenerate "<command>"]    # only if they asked
```

Setup writes `plowline.toml`, checks that the gate passes, adds the lane rules
to `AGENTS.md` and `CLAUDE.md` (between them, read by most CLI agents), writes
`briefs/TEMPLATE.md` and a dispatcher skill at `.claude/skills/plowline/SKILL.md`,
and commits them. It never pushes unless you add `--push`.

Show the person `git show --stat HEAD`, then ask before pushing. Lanes branch
from the remote main, so they only see the setup once it's pushed:

```bash
git push origin HEAD:main     # use the trunk name from plowline.toml
```

In local-only mode there's nothing to push.

## Part 4: Teach

Offer a five-minute tour. If they say yes:

1. **Run the demo** and walk through its output. It runs five lanes and two
   trains in a throwaway repo, then deletes it:

   ```bash
   pl demo
   ```

   Point out the three outcomes: lanes a, b and c land together. Lane d
   conflicts with c and bounces, then its agent merges main, fixes it and lands
   on the next train. Lane e breaks the tests and bounces, and it doesn't stop
   the others.

2. **Explain the daily loop** in their repo, with this cheat sheet:

   | Step | Command | Who |
   |---|---|---|
   | Start a lane with a brief, in a new terminal | `pl new <lane> --paths <files> --brief briefs/<lane>.md --open` | the person, or you |
   | Keep a control tower open | `pl watch` | the person, one terminal |
   | Finish a lane | `pl done <lane>` | the lane's agent |
   | Fix a bounced lane | `pl resume <lane>` | the lane's agent |
   | Tidy up | `pl clean` | anyone |

3. **Tell them they can delegate the dispatching to you.** From now on, in this
   repo, they can say "split this into lanes" or "how are the lanes doing?" to
   their agent in the main checkout. The dispatcher skill tells it how
   to plan lanes, write briefs, start them, and report back.

4. **Offer to plan their first real lanes.** Ask what they want done next, and
   follow `.claude/skills/plowline/SKILL.md`: propose two or three small lanes
   that touch different files, write their briefs, and start them once the
   person agrees.

If they work alone with several terminals, point them to
https://github.com/oxgate-inc/plowline/blob/main/docs/SOLO.md.

A person who would rather answer prompts themselves can run `pl setup` with no
flags. It's the same steps as an interactive wizard.
