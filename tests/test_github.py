import unittest

from factory.core.github import FakeGitHub, GitHubError


class FakeGitHubTests(unittest.TestCase):
    def setUp(self):
        self.gh = FakeGitHub()
        self.gh.add_issue(1, title="Add subtract", author="alice")

    def test_issue_round_trip(self):
        self.assertEqual(self.gh.issue(1)["title"], "Add subtract")

    def test_comments_and_labels_are_recorded(self):
        self.gh.comment("issue", 1, "hello")
        self.gh.label("issue", 1, add="factory")
        self.assertEqual(self.gh.comments, [("issue", 1, "hello")])
        self.assertEqual(self.gh.labels, [("issue", 1, "factory", None)])

    def test_merge_sets_state(self):
        self.gh.merge_pr(7, "squash")
        self.assertEqual(self.gh.pr_state(7), "MERGED")
        self.assertEqual(self.gh.merged, [(7, "squash")])

    def test_missing_issue_raises(self):
        with self.assertRaises(GitHubError):
            self.gh.issue(999)


if __name__ == "__main__":
    unittest.main()
