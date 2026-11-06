---
name: plowline
description: Run work in parallel Plowline lanes in this repository. Use when the user asks to split work across agents or lanes, start, check or land lanes, run a train, or deal with a bounced lane.
---

# Dispatching Plowline lanes

You are the dispatcher: the session in the main checkout, not in a lane. Lanes
are separate agents, each in its own worktree. You plan them, start them, watch
them land, and tell the user what happened. You do not write the lanes' code.

## Plan the lanes

1. Break the user's request into lanes. A good lane is one feature or fix,
   about an hour or two of agent work, touching files no other lane touches.
   Two to four lanes at once suits one person.
2. If two pieces must change the same files, make them one lane or run them
   one after the other.
3. For each lane, write `briefs/<lane>.md` from `briefs/TEMPLATE.md`: the goal,
   the files it owns, what done looks like, and how to test it.
4. Show the user the plan (lane names, files, one line each) and wait for an OK
   before starting anything.

## Start and watch

```bash
pl new <lane> --paths <files or dirs> --brief briefs/<lane>.md --open
pl status        # states: active, candidate, proposed, bounced, landed
pl train         # land every candidate now (or let `pl watch` do it)
```

`--open` starts the lane's agent in a new terminal. If it prints a command
instead, give that command to the user. An overlap warning means two lanes
claim the same files: tell the user and suggest merging the lanes.

Run `pl status` when the user asks how things are going, and after a lane's
agent says it ran `pl done`. Run `pl train` when lanes are candidates, unless
the user is running `pl watch` in another terminal.

## When something goes wrong

- **Bounced lane**: `pl show <lane>` gives the reason. Tell the user which lane
  and why, and that the lane's agent should run `pl resume <lane>` and fix it.
  Do not fix it from the main checkout.
- **NO-GO from review**: the lane's agent already has the findings. Nothing for
  you to do unless the user asks.
- **`another train holds ... train.lock`**: another train is running. Wait. Only
  delete the lock if the user confirms nothing else is running.
- **Push refused**: the trunk moved. Run `pl train` again.

## Finish

When every lane has landed, run `pl clean` and give the user a short summary:
which lanes landed, at which commit, and anything still bounced.

## Never

- Never edit files inside a lane's worktree, or commit to a lane branch.
- Never push to the trunk yourself. The train is the only thing that writes to it.
- Never rebase a lane.
