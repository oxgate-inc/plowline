"""Pure rules the train and the lanes follow. No git, no files: easy to test, easy to read."""
from __future__ import annotations

import fnmatch
import re
from typing import Callable, Sequence, TypeVar

T = TypeVar("T")

# Reviewers are language models writing markdown, full of typographic dashes. "NO‑GO" must never read
# as GO, so every Unicode dash is folded to "-" before the verdict is parsed.
DASHES = dict.fromkeys(map(ord, "‐‑‒–—―−﹘﹣－"), "-")
GO_WORD = re.compile(r"(?<![-\w])GO(?![-\w])")
VERDICT_LINES = 8


def fold_dashes(text: str) -> str:
    return text.translate(DASHES)


def verdict_token(text: str) -> str:
    """GO, NO-GO or "" from the first eight lines of a review. NO-GO is read first, so it always wins."""
    head = fold_dashes("\n".join(text.splitlines()[:VERDICT_LINES])).upper()
    if "NO-GO" in head:
        return "NO-GO"
    return "GO" if GO_WORD.search(head) else ""


class TrunkRed(Exception):
    """The gate is red on the trunk alone: no lane can be blamed for it."""


def find_green(members: Sequence[T], gate: Callable[[list[T]], bool],
               on_blame: Callable[[T, list[T]], None] | None = None) -> tuple[list[T], list[T]]:
    """Split a red train until the merged set is green.

    gate(subset) builds the trunk plus `subset` (in order) and answers whether the gate passed. Returns the
    largest set found green, in train order, and the lanes that turned it red. A lane is blamed only when
    the gate went red with it on top of a set already proven green. With two members the first probe is
    "drop the last one". If the bare trunk is red, TrunkRed is raised and nobody is blamed.
    on_blame(lane, accepted) is told the green set each lane was blamed on top of; that exact set plus the
    lane is always one that was gated red.
    """
    members = list(members)
    if gate(members):
        return members, []
    culprits: list[T] = []
    trunk: list[bool] = []

    def blame(accepted: list[T], lane: T) -> None:
        if not accepted and not trunk:
            trunk.append(gate([]))
            if not trunk[0]:
                raise TrunkRed()
        culprits.append(lane)
        if on_blame:
            on_blame(lane, list(accepted))

    def settle(accepted: list[T], group: list[T]) -> list[T]:
        """gate(accepted + group) is known red and accepted is known green."""
        if len(group) == 1:
            blame(accepted, group[0])
            return accepted
        half = (len(group) + 1) // 2
        left, right = group[:half], group[half:]
        if gate(accepted + left):
            return settle(accepted + left, right)
        accepted = settle(accepted, left)
        if gate(accepted + right):
            return accepted + right
        return settle(accepted, right)

    return settle([], members), culprits


def merge_tree_paths(output: str) -> list[str]:
    """Conflicted paths from `git merge-tree --write-tree --name-only`: the lines after the tree id, up to the
    first blank line."""
    paths = []
    for line in output.splitlines()[1:]:
        if not line.strip():
            break
        paths.append(line.strip())
    return paths


def git_version(text: str) -> tuple[int, int]:
    nums = re.findall(r"\d+", text)
    return (int(nums[0]), int(nums[1])) if len(nums) >= 2 else (0, 0)


def _norm(path: str) -> str:
    return path.replace("\\", "/").removeprefix("./").rstrip("/")


def in_claim(path: str, claims: Sequence[str]) -> bool:
    """Whether a changed file lies inside a lane's claimed paths (a file, a directory, or a glob)."""
    path = _norm(path)
    for claim in claims:
        c = _norm(claim)
        if c and (path == c or path.startswith(c + "/") or fnmatch.fnmatchcase(path, c)):
            return True
    return False


def is_generated(path: str, patterns: Sequence[str]) -> bool:
    """A generated file: a pattern with a slash matches the whole path; a bare name matches at any depth."""
    path = _norm(path)
    name = path.rsplit("/", 1)[-1]
    for pattern in patterns:
        p = _norm(pattern)
        if p and (fnmatch.fnmatchcase(path, p) or ("/" not in p and fnmatch.fnmatchcase(name, p))):
            return True
    return False


FOLLOWUP_HEADING = re.compile(r"^#{1,6}\s*(follow[- ]?ups?|non[- ]?blocking.*)\s*$", re.I)
HEADING = re.compile(r"^#{1,6}\s")
ITEM = re.compile(r"^\s*[-*]\s+(.*\S)")
NONE_ITEMS = {"none", "none.", "n/a", "nothing"}


def extract_followups(review: str) -> list[str]:
    """The items under a "Follow-ups" (or "Non-blocking") heading of a review."""
    items, inside = [], False
    for line in fold_dashes(review).splitlines():
        if FOLLOWUP_HEADING.match(line.strip()):
            inside = True
            continue
        if HEADING.match(line):
            inside = False
            continue
        m = ITEM.match(line) if inside else None
        if m and m.group(1).strip().lower() not in NONE_ITEMS:
            items.append(m.group(1).strip())
    return items
