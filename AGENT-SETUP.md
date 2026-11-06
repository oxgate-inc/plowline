# Setting up Plowline (instructions for a coding agent)

You are setting up Plowline in the user's repository so that several coding
agents can work on it at once. Follow these steps in order. Stop and ask the user
only where a step says to.

## 1. Check the prerequisites

```bash
git --version      # need 2.31 or newer
python3 --version  # need 3.11 or newer (on Windows: python --version)
git remote -v      # the train pushes to a remote, normally origin
```

If there is no remote, stop and ask the user which remote the train should push to.

## 2. Install

```bash
pipx install git+https://github.com/oxgate-inc/plowline
```

If `pipx` is missing, use `python3 -m pip install --user git+https://github.com/oxgate-inc/plowline`.
Confirm with `yard --help`.

## 3. Write the config

From the repository root:

```bash
yard init
```

Open `plowline.toml` and set `gate` to the command that proves the repository
works: the full test suite, plus the build or lint step if CI runs one. Use what
CI already runs (look in `.github/workflows/`, `Makefile`, `package.json`,
`pyproject.toml`). Run the gate once on a clean checkout and confirm it passes.
If it fails before any lane exists, stop and tell the user. The train cannot
work on a red trunk.

Set `review`:
- `"builtin:claude"` if `claude` is on PATH, or `"builtin:codex"` if `codex` is.
- `""` if the user wants no automated review.

If the repository has generated files that most changes touch (lockfiles,
generated docs, schema snapshots), set `regenerate` to the command that rebuilds them.

## 4. Add the lane rules for agents

Append [templates/AGENTS-snippet.md](templates/AGENTS-snippet.md) to `AGENTS.md`
(Codex and most agents read it) and to `CLAUDE.md` (Claude Code reads it).
Create either file if it does not exist. Copy
[templates/lane-brief.md](templates/lane-brief.md) to `briefs/TEMPLATE.md`.

## 5. Commit on the trunk

```bash
git add plowline.toml AGENTS.md CLAUDE.md briefs/TEMPLATE.md
git commit -m "Add Plowline lane setup"
```

Show the user the diff and ask before pushing. Lanes branch from the remote
trunk, so they only see the config after this commit is pushed.

## 6. Prove it

Clone Plowline to a scratch directory and run its demo. It builds a throwaway
repository and touches nothing of the user's:

```bash
git clone https://github.com/oxgate-inc/plowline /tmp/plowline
python3 /tmp/plowline/examples/demo.py
```

Expect three lanes to land, one to bounce on a conflict and land after
`yard resume`, and one to bounce on a red gate.

## 7. Tell the user how to run lanes

Give them this, with their own lane names:

```bash
yard new <lane> --paths <dirs or files> --brief briefs/<lane>.md
# start one agent per lane, in the worktree that `yard new` printed
yard done <lane>     # when the agent has committed
yard train           # lands every candidate, one push
yard status
```
