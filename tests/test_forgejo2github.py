"""Regression tests for forgejo2github.py.

Run with:  python3 -I -m unittest discover -s ~/.claude/skills/forgejo-to-github/tests -v

The live tests only read from Codeberg. Their reference is the archived
codeberg.org/stella10x1/dotfiles-nvim, the first repository migrated with this
script (2026-11-07, Forgejo 17.0.0-93-4d598f4+gitea-1.22.0). Its plan was
checked against the issues that migration created on GitHub and is stored in
fixtures/. The milestone, release and link checks use the archived
codeberg.org/lingfra/ansible-collection-lingfra-podman (migrated the same
day), which has a milestone and a release. If Codeberg runs a different
Forgejo version and these tests still pass, the script works as it did on the
tested version.

Set FORGEJO2GITHUB_OFFLINE=1 to run only the offline tests.
"""
import importlib.util
import json
import os
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("forgejo2github", HERE.parent / "scripts" / "forgejo2github.py")
assert spec and spec.loader
f2g = importlib.util.module_from_spec(spec)
spec.loader.exec_module(f2g)

REFERENCE = "https://codeberg.org/stella10x1/dotfiles-nvim"
REFERENCE_GITHUB = "stella10x1/dotfiles-nvim"
REFERENCE_OLD_OWNERS = ["lingling"]
EXTRAS = "https://codeberg.org/lingfra/ansible-collection-lingfra-podman"
EXTRAS_GITHUB = "lingfra/ansible-collection-lingfra-podman"
OFFLINE = os.environ.get("FORGEJO2GITHUB_OFFLINE") == "1"


class Offline(unittest.TestCase):
    def test_source_url_forms(self):
        for url in (
            "https://codeberg.org/alice/proj",
            "https://codeberg.org/alice/proj.git",
            "https://codeberg.org/alice/proj/issues/5",
            "git@codeberg.org:alice/proj.git",
            "ssh://git@codeberg.org:2222/alice/proj.git",
        ):
            src = f2g.Source(url)
            self.assertEqual((src.host, src.full, src.forge), ("codeberg.org", "alice/proj", "Codeberg"), url)
        self.assertEqual(f2g.Source("https://git.example.org/a/b").forge, "git.example.org")

    def test_rewrite_links(self):
        src = f2g.Source("https://codeberg.org/alice/proj")
        cases = {
            "https://codeberg.org/alice/proj/src/branch/dev/x.lua": "https://github.com/bob/proj/tree/dev/x.lua",
            "https://codeberg.org/alice/proj/issues/3#issuecomment-1": "https://github.com/bob/proj/issues/3#issuecomment-1",
            "https://codeberg.org/alice/proj/pulls/4": "https://github.com/bob/proj/issues/4",
            "https://codeberg.org/alice/proj/commit/abc123": "https://github.com/bob/proj/commit/abc123",
            "https://codeberg.org/old/proj/src/branch/x": "https://github.com/bob/proj/tree/x",
            "https://codeberg.org/old/other": "https://codeberg.org/alice/other",
            "[a](/attachments/u1)": "[a](https://codeberg.org/attachments/u1)",
            "https://codeberg.org/carol/proj/issues/1": "https://codeberg.org/carol/proj/issues/1",
            "https://codeberg.org/alice/proj-two/issues/1": "https://codeberg.org/alice/proj-two/issues/1",
        }
        for before, after in cases.items():
            self.assertEqual(f2g.rewrite_links(before, src, "bob/proj", ["old"]), after, before)

    def test_rewrite_file_links(self):
        src = f2g.Source("https://codeberg.org/alice/proj")
        cases = {
            "https://codeberg.org/alice/proj": "https://github.com/bob/proj",
            "see https://codeberg.org/alice/proj.": "see https://github.com/bob/proj.",
            "url = https://codeberg.org/alice/proj.git": "url = https://github.com/bob/proj.git",
            "url = git@codeberg.org:alice/proj.git": "url = git@github.com:bob/proj.git",
            "ssh://git@codeberg.org/alice/proj.git": "ssh://git@github.com/bob/proj.git",
            "(https://codeberg.org/alice/proj/)": "(https://github.com/bob/proj/)",
            "https://codeberg.org/alice/proj/issues": "https://github.com/bob/proj/issues",
            "https://codeberg.org/alice/proj/issues/3": "https://github.com/bob/proj/issues/3",
            "https://codeberg.org/alice/proj/pulls/4": "https://github.com/bob/proj/issues/4",
            "https://codeberg.org/alice/proj/compare/v1..HEAD": "https://github.com/bob/proj/compare/v1..HEAD",
            "https://codeberg.org/alice/proj/releases/tag/v1": "https://github.com/bob/proj/releases/tag/v1",
            "https://codeberg.org/alice/proj/src/branch/main/a.md#x": "https://github.com/bob/proj/tree/main/a.md#x",
            "https://codeberg.org/alice/proj/raw/branch/main/a.md": "https://github.com/bob/proj/raw/main/a.md",
            "https://codeberg.org/old/proj/issues/1": "https://github.com/bob/proj/issues/1",
            "https://codeberg.org/old/other": "https://codeberg.org/alice/other",
            # Paths without a GitHub counterpart, other repositories and relative links stay.
            "https://codeberg.org/alice/proj/wiki/Home": "https://codeberg.org/alice/proj/wiki/Home",
            "https://codeberg.org/alice/proj-two": "https://codeberg.org/alice/proj-two",
            "https://codeberg.org/alice/proj.lua": "https://codeberg.org/alice/proj.lua",
            "git@codeberg.org:alice/other.git": "git@codeberg.org:alice/other.git",
            "[a](/docs/a.md)": "[a](/docs/a.md)",
        }
        for before, after in cases.items():
            self.assertEqual(f2g.rewrite_file_links(before, src, "bob/proj", ["old"]), after, before)

    def test_plan_file_links(self):
        src = f2g.Source("https://codeberg.org/alice/proj")
        files = {
            "README.md": "clone https://codeberg.org/alice/proj.git\nsee https://codeberg.org/alice/lib\n",
            "a.j2": "[x]: https://codeberg.org/alice/{{ name }}\n",
            "b.txt": "nothing\n",
        }
        new_texts, changed, left = f2g.plan_file_links(files, src, "bob/proj")
        self.assertEqual(list(new_texts), ["README.md"])
        self.assertEqual(changed, [("README.md", 1, "https://codeberg.org/alice/proj.git", "https://github.com/bob/proj.git")])
        self.assertEqual(
            left, [("README.md", 2, "https://codeberg.org/alice/lib"), ("a.j2", 1, "https://codeberg.org/alice/{{")]
        )
        self.assertEqual(f2g.url_repo("git@codeberg.org:alice/lib.git", src), "alice/lib")
        self.assertIsNone(f2g.url_repo("https://codeberg.org/alice", src))

    def test_website_target(self):
        self.assertEqual(f2g.website_target("https://github.com/lingfra/aar-doc-config"), "lingfra/aar-doc-config")
        self.assertEqual(f2g.website_target("https://github.com/a/b.git/"), "a/b")
        for website in ("", None, "https://galaxy.ansible.com/lingfra/podman", "https://github.com/a"):
            self.assertIsNone(f2g.website_target(website), website)

    def test_neutralize_mentions(self):
        text = "\n".join(["thanks @carol and @d.e-f.", "mail a@b.org", "`@code` stays", "```", "@fenced", "```"])
        self.assertEqual(
            f2g.neutralize_mentions(text),
            "\n".join(["thanks `@carol` and `@d.e-f`.", "mail a@b.org", "`@code` stays", "```", "@fenced", "```"]),
        )

    def test_sanitize_topic(self):
        self.assertEqual(f2g.sanitize_topic("Neovim.Lua"), "neovim-lua")
        self.assertEqual(f2g.sanitize_topic("-x_y-"), "x-y")

    def test_tested_version_is_noted_in_skill_md(self):
        self.assertIn(f2g.TESTED_FORGEJO_VERSION, (HERE.parent / "SKILL.md").read_text())


@unittest.skipIf(OFFLINE, "FORGEJO2GITHUB_OFFLINE=1")
class Live(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.src = f2g.Source(REFERENCE)
        cls.version = cls.src.version()

    def test_forgejo_version(self):
        if self.version != f2g.TESTED_FORGEJO_VERSION:
            self.skipTest(f"Forgejo version CHANGED: {f2g.TESTED_FORGEJO_VERSION} -> {self.version}")

    def test_api_fields(self):
        src = self.src
        repo = src.api(f"repos/{src.full}")
        for key in ("description", "website", "private", "archived", "default_branch", "has_issues", "has_wiki"):
            self.assertIn(key, repo)
        self.assertIsInstance(src.api(f"repos/{src.full}/topics", {"limit": 50})["topics"], list)
        for lbl in src.paged(f"repos/{src.full}/labels"):
            self.assertTrue({"id", "name", "color", "description"} <= lbl.keys())
        issues = {i["number"]: i for i in src.paged(f"repos/{src.full}/issues", state="all")}
        # Without a type filter the issues endpoint must also return pull requests.
        self.assertIsNotNone(issues[16]["pull_request"])
        self.assertIsNone(issues[15]["pull_request"])
        for key in ("id", "number", "title", "body", "state", "labels", "comments", "created_at", "user", "milestone"):
            self.assertIn(key, issues[5])
        pr = src.api(f"repos/{src.full}/pulls/16")
        self.assertTrue(pr["merged"])
        for key in ("merged_at", "closed_at"):
            self.assertIn(key, pr)
        self.assertIn("ref", pr["head"])
        self.assertIn("ref", pr["base"])
        for c in src.paged(f"repos/{src.full}/issues/5/comments"):
            self.assertTrue({"id", "body", "created_at", "html_url", "user"} <= c.keys())
        self.assertRegex(src.api(f"repos/{src.full}/branches/{repo['default_branch']}")["commit"]["id"], r"^[0-9a-f]{40}$")

    def test_pagination(self):
        page = f2g.PAGE
        setattr(f2g, "PAGE", 5)
        try:
            self.assertEqual(len(self.src.paged(f"repos/{self.src.full}/issues", state="all")), 16)
            self.assertEqual(len(self.src.paged(f"repos/{self.src.full}/issues/5/comments")), 3)
        finally:
            setattr(f2g, "PAGE", page)

    def test_plan_matches_snapshot(self):
        expected = json.loads((HERE / "fixtures" / "codeberg-dotfiles-nvim-plan.json").read_text())
        issues, plan = f2g.plan_issues(self.src, REFERENCE_GITHUB, REFERENCE_OLD_OWNERS)
        self.assertEqual(f2g.plan_labels(self.src, issues), expected["labels"])
        self.assertEqual(len(plan), len(expected["issues"]))
        for got, want in zip(plan, expected["issues"]):
            self.assertEqual(got, want, f"#{want['number']}")

    def test_milestones_and_releases(self):
        src = f2g.Source(EXTRAS)
        self.assertEqual(
            f2g.plan_milestones(src, EXTRAS_GITHUB),
            [{"title": "0.2", "description": "", "state": "open", "due_on": None, "issues": [6]}],
        )
        releases = f2g.plan_releases(src, EXTRAS_GITHUB)
        self.assertEqual(len(releases), 1)
        r = releases[0]
        self.assertEqual(
            {k: r[k] for k in ("tag", "name", "draft", "prerelease", "published", "assets")},
            {"tag": "v0.1.0", "name": "v0.1.0 Proof of concept", "draft": False, "prerelease": True,
             "published": "2025-02-02", "assets": []},
        )
        self.assertTrue(r["body"].startswith("This version initializes the collection"))
        self.assertNotIn("\r", r["body"])

    def test_link_exists(self):
        src = f2g.Source(EXTRAS)
        cases = {
            "/src/branch/main/roles/podman_construct#x": True,
            "/src/branch/poc/roles/podman_construct": False,  # branch deleted before the migration
            "/src/branch/main/missing.md": False,
            "/src/tag/v0.1.0/README.md": True,
            "/src/commit/0f67234/galaxy.yml": True,
            "/commit/0f67234": True,
            "/issues/3": True,
            "/issues/99": False,
            "/pulls/2": True,
            "/releases/tag/v0.1.0": True,
            "/releases/tag/v9.9.9": False,
            "/compare/v0.1.0..HEAD": None,
        }
        for path, want in cases.items():
            self.assertIs(src.link_exists(EXTRAS + path), want, path)


if __name__ == "__main__":
    unittest.main()
