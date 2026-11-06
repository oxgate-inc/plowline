# Setting up Plowline (instructions for a coding agent)

You are setting up Plowline in the user's repository so that several coding
agents can work on it at once. Follow these steps in order. Stop and ask the user
only where a step says to.

## 1. Check the prerequisites

```bash
git --version      # need 2.31 or newer
python --version   # need 3.11 or newer (python3 on some systems)
git remote -v      # the train pushes to a remote, normally origin
git status         # should be clean, on the trunk branch
```

If there is no remote, stop and ask the user which remote the train should push to.
If the working tree is dirty, stop and ask the user what to do with the changes.

## 2. Install

```bash
pipx install git+https://github.com/oxgate-inc/plowline
```

If `pipx` is missing, use `python -m pip install git+https://github.com/oxgate-inc/plowline`.
Confirm with `pl --help`.

## 3. Find the gate

The gate is the command that proves the repository works. Every train must pass
it before anything is pushed. Use what CI already runs: look in
`.github/workflows/`, `Makefile`, `package.json`, `pyproject.toml`. Prefer the
full test suite, plus the build or lint step if CI runs one. Run it once and
confirm it passes. If it fails before any lane exists, stop and tell the user.

## 4. Run setup unattended

```bash
pl setup --yes --commit --gate "<the gate command>"
```

It writes `plowline.toml`, checks the gate passes, adds the lane rules to
`AGENTS.md` and `CLAUDE.md`, writes `briefs/TEMPLATE.md`, and commits those
files. It never pushes unless you add `--push`. Options:

- `--review builtin:claude` or `--review builtin:codex` picks the reviewer. By
  default it uses whichever CLI is on PATH. `--review ""` turns review off.
- `--regenerate "<command>"` if most changes touch generated files (lockfiles,
  generated docs, schema snapshots) and a command rebuilds them.

## 5. Ask before pushing

Show the user `git show --stat HEAD` and ask whether to push it. Lanes branch
from the remote trunk, so they only see the setup after it is pushed:

```bash
git push origin HEAD:main     # use the trunk name from plowline.toml
```

## 6. Tell the user how to run lanes

```bash
pl new <lane> --paths <dirs or files> --brief briefs/<lane>.md --open
# --open starts the agent in a new terminal, already in its lane
pl watch           # control tower: status, and a train whenever a lane is done
```

Each lane's agent runs `pl done <lane>` itself when its work is committed.
If the user works alone with several terminals, point them to
https://github.com/oxgate-inc/plowline/blob/main/docs/SOLO.md.

A person can run `pl setup` with no flags for the same steps as an
interactive wizard.
