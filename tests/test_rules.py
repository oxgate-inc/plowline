"""Pure rules: verdict parsing, bisection, merge previews, claims, follow-ups."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from plowline import rules  # noqa: E402


class VerdictTest(unittest.TestCase):
    def test_no_go_is_read_before_go(self):
        self.assertEqual(rules.verdict_token("NO-GO\n- a bug"), "NO-GO")
        self.assertEqual(rules.verdict_token("# Review: NO-GO"), "NO-GO")
        self.assertEqual(rules.verdict_token("GO\nlooks fine"), "GO")
        self.assertEqual(rules.verdict_token("**GO**"), "GO")

    def test_unicode_dashes_are_folded_before_parsing(self):
        for dash in "‐‑‒–—―−－":
            self.assertEqual(rules.verdict_token(f"NO{dash}GO"), "NO-GO", repr(dash))

    def test_no_go_written_without_a_hyphen_is_still_no_go(self):
        for text in ("NO GO", "No go: broken", "Verdict: not GO", "NOGO", "no  go", "Verdict: NOT go\nGO later",
                     "NO—GO", "**No Go**"):
            self.assertEqual(rules.verdict_token(text), "NO-GO", repr(text))

    def test_go_is_still_go(self):
        for text in ("GO", "Verdict: GO", "go\nall good", "# Review: GO\n\n## Blocking\nnone"):
            self.assertEqual(rules.verdict_token(text), "GO", repr(text))

    def test_go_needs_word_boundaries(self):
        self.assertEqual(rules.verdict_token("GOOD work\nGOTCHA"), "")
        self.assertEqual(rules.verdict_token("ALGO"), "")
        self.assertEqual(rules.verdict_token(""), "")

    def test_only_the_first_eight_lines_count(self):
        self.assertEqual(rules.verdict_token("\n" * 8 + "GO"), "")
        self.assertEqual(rules.verdict_token("GO\n" + "x\n" * 10 + "NO-GO"), "GO")


class BisectTest(unittest.TestCase):
    """find_green over a fake gate: red whenever a bad lane is in the set."""

    def run_bisect(self, members, bad, trunk_red=False):
        calls = []

        def gate(subset):
            calls.append(tuple(subset))
            return not trunk_red and not any(m in bad for m in subset)
        green, culprits = rules.find_green(members, gate)
        return green, culprits, calls

    def test_all_green_is_one_run(self):
        green, culprits, calls = self.run_bisect(list("abcd"), set())
        self.assertEqual((green, culprits, len(calls)), (list("abcd"), [], 1))

    def test_two_members_probe_drop_last_first(self):
        green, culprits, calls = self.run_bisect(["a", "b"], {"b"})
        self.assertEqual(calls[:2], [("a", "b"), ("a",)])
        self.assertEqual((green, culprits), (["a"], ["b"]))

    def test_two_members_first_is_red(self):
        green, culprits, calls = self.run_bisect(["a", "b"], {"a"})
        self.assertEqual((green, culprits), (["b"], ["a"]))
        self.assertIn((), calls)  # blamed against the bare trunk: the trunk itself was checked

    def test_one_bad_lane_among_many(self):
        for bad in "abcdefgh":
            green, culprits, calls = self.run_bisect(list("abcdefgh"), {bad})
            self.assertEqual(culprits, [bad])
            self.assertEqual(green, [m for m in "abcdefgh" if m != bad])
            self.assertLessEqual(len(calls), 9)

    def test_several_bad_lanes(self):
        green, culprits, _ = self.run_bisect(list("abcdefg"), {"b", "f", "g"})
        self.assertEqual(culprits, ["b", "f", "g"])
        self.assertEqual(green, list("acde"))

    def test_everything_red(self):
        green, culprits, _ = self.run_bisect(list("abc"), set("abc"))
        self.assertEqual((green, culprits), ([], list("abc")))

    def test_a_red_trunk_blames_nobody(self):
        with self.assertRaises(rules.TrunkRed):
            self.run_bisect(list("abc"), set(), trunk_red=True)

    def test_every_green_answer_is_a_set_that_was_gated_green(self):
        calls = []
        bad = {"c", "e"}

        def gate(subset):
            calls.append(tuple(subset))
            return not any(m in bad for m in subset)
        green, _ = rules.find_green(list("abcdef"), gate)
        self.assertIn(tuple(green), calls)


class MergeTreeTest(unittest.TestCase):
    def test_conflict_paths_are_the_lines_between_the_tree_and_the_blank_line(self):
        out = ("4b825dc642cb6eb9a060e54bf8d69288fbee4904\nshared.txt\nweb/package-lock.json\n\n"
               "Auto-merging shared.txt\nCONFLICT (content): Merge conflict in shared.txt\n")
        self.assertEqual(rules.merge_tree_paths(out), ["shared.txt", "web/package-lock.json"])

    def test_clean_merge_has_no_paths(self):
        self.assertEqual(rules.merge_tree_paths("4b825dc642cb6eb9a060e54bf8d69288fbee4904\n"), [])

    def test_git_version(self):
        self.assertEqual(rules.git_version("git version 2.53.0.windows.2"), (2, 53))
        self.assertEqual(rules.git_version("git version 2.37.1 (Apple Git-137)"), (2, 37))


class ClaimTest(unittest.TestCase):
    def test_in_claim(self):
        self.assertTrue(rules.in_claim("src/auth/login.py", ["src/auth/"]))
        self.assertTrue(rules.in_claim("src/app.py", ["src/*.py"]))
        self.assertTrue(rules.in_claim("docs/a.md", ["./docs/a.md"]))
        self.assertFalse(rules.in_claim("src/api.py", ["src/app.py"]))
        self.assertFalse(rules.in_claim("src/authz.py", ["src/auth"]))

    def test_generated_patterns(self):
        gen = ["package-lock.json", "docs/schema/*.json"]
        self.assertTrue(rules.is_generated("package-lock.json", gen))
        self.assertTrue(rules.is_generated("web/package-lock.json", gen))  # a bare name matches at any depth
        self.assertTrue(rules.is_generated("docs/schema/user.json", gen))
        self.assertFalse(rules.is_generated("src/package.json", gen))
        self.assertFalse(rules.is_generated("x", []))


class FollowupsTest(unittest.TestCase):
    def test_items_under_a_follow_ups_heading(self):
        out = ("GO\n\n## Blocking\nnone\n\n## Follow-ups\n- rename helper\n* add a docstring\n\n"
               "## Notes\n- not a follow-up\n")
        self.assertEqual(rules.extract_followups(out), ["rename helper", "add a docstring"])

    def test_non_blocking_heading_and_none(self):
        self.assertEqual(rules.extract_followups("GO\n### Non-blocking\n- one\n"), ["one"])
        self.assertEqual(rules.extract_followups("GO\n## Follow-ups\n- none\n"), [])
        self.assertEqual(rules.extract_followups("GO\nall good"), [])


if __name__ == "__main__":
    unittest.main()
