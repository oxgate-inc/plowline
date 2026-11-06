## Working in a Plowline lane

This repository runs several agents at once. Each agent works in its own lane.

- Work only inside the worktree your lane was given. Never edit the main checkout
  or another lane's worktree.
- Commit to your lane branch (`lane/<name>`). Never push to the trunk yourself.
  The merge train is the only thing that writes to it.
- Stay inside the paths your brief claims. If you need a file outside them, say
  so in your final report instead of editing it.
- Never rebase. To pick up the trunk, merge it: `git merge origin/main`.
- Run the repository's tests before you finish. Leave the worktree clean
  (everything committed).
- When your work is committed and the tests pass, run `pl done <lane>`. It sends
  your lane to review. On NO-GO, fix the findings, commit, and run it again.
  Then report what changed and how you tested it.
- If your lane was bounced, `pl show <lane>` prints why. Merge the trunk,
  fix the cause, re-test, commit, and run `pl done <lane>` again.
