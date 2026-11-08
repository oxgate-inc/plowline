## Working in a Plowline lane

This repository runs several agents at once. Each agent works in its own lane.
If you are in the main checkout rather than a lane, and the user wants work
split across agents, follow `.claude/skills/plowline/SKILL.md` instead.

- Work only inside the worktree your lane was given. Never edit the main checkout
  or another lane's worktree.
- Commit to your lane branch (`lane/<name>`). Never push to the trunk yourself.
  The merge train is the only thing that writes to it.
- Stay inside the paths your brief claims. `pl done` lists any file you changed
  outside them, and some repositories bounce the lane for it. If you need a file
  outside the claim, say so in your final report instead of editing it.
- Never rebase. To pick up the trunk, merge it: `git merge origin/main`.
- Run the repository's tests before you finish. Leave the worktree clean
  (everything committed): `pl done` refuses a worktree with uncommitted changes.
- When your work is committed and the tests pass, run `pl done <lane>`. It sends
  your lane to review. On NO-GO, fix the findings, commit, and run it again.
- End your final report with this block, filled in. A claim without the command
  that proved it is not evidence:

      COMMIT: <the commit you finished on>
      COMMANDS RUN: <each command you ran, with its exit code>
      RESULT: <what passed or failed, with counts>
      NOT VERIFIED: <what you could not check, or "nothing">

- If your lane was bounced, `pl resume <lane>` prints the notice: why, the
  conflicting paths or the gate output, and what to do. Merge the trunk, fix the
  cause, re-test, commit, and run `pl done <lane>` again.
