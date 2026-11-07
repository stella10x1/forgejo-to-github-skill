# Moved-to-GitHub notice

Two commits on top of the source default branch:

1. **Notice commit**, pushed to Forgejo as branch `moved-to-github` for the
   user to review, and part of GitHub's history.
2. **Removal commit**, only on GitHub, directly on top of the notice commit.

Since GitHub's default branch is "Forgejo default branch + notice + removal",
Forgejo can later be fast-forwarded to GitHub if the project ever moves back.
This only holds if the notice reaches Forgejo's default branch as the very
same commit (fast-forward), not rebased or squashed.

## Notice commit

Put the box directly below the README's top-level heading (or at the top if
there is none). Also point install/clone instructions and badges that use the
Forgejo URL at GitHub. Those URL changes stay in the removal commit, only the
box goes away again.

If there is no README, create `README.md` with a `# <repo name>` heading and
the box. The removal commit then deletes the file again.

Match the README's markup: the template is Markdown with a GitHub/Forgejo
alert (`> [!WARNING]`). For AsciiDoc use `[WARNING]` with a `====` block,
for Org mode `#+begin_warning`, for reStructuredText `.. warning::`.

### Codeberg

This is the wording merged on codeberg.org/stella10x1/dotfiles-nvim. Fill
in the GitHub URL. Keep the last sentence only if it is true for this
repository (ask the user if unsure).

```markdown
> [!WARNING]
> **This repository has moved to GitHub:
> [github.com/OWNER/NAME](https://github.com/OWNER/NAME).**
> The Codeberg repository is no longer maintained. All issues and pull requests
> have been migrated, keeping their numbers.
>
> In July 2026 the Codeberg e.V. assembly
> [voted](https://blog.codeberg.org/protecting-our-floss-commons-from-llms.html)
> to extend Codeberg's Terms of Use with
> [§ 2 (1) 7](https://codeberg.org/Codeberg/org/commit/71149c7fc95ccfeae36109b5cddca339e4aa1473),
> which prohibits sharing projects that mostly consist of code written by
> generative AI tools such as Claude or OpenAI Codex. As I use AI coding
> assistants to work on this configuration, I moved it to GitHub.
```

"this configuration" fits a dotfiles repository; use "this project" (or
whatever fits) elsewhere. Background: the rule says *"You must not share
projects that mostly consist of code written by "generative AI"-tools
(including services such as Claude, OpenAI Codex)."* It was merged on
2026-07-22 in Codeberg/org#1253.

### Other Forgejo instances

The Codeberg Terms of Use don't apply there. Keep the first paragraph
(replace "Codeberg" with the instance name) and ask the user for the reason,
or leave the second paragraph out.

### Commit message

```
Add moved-to-GitHub note to README

The repository moved to GitHub: <one-line reason, e.g. Codeberg's Terms of
Use now prohibit projects mostly written by generative AI tools>. This
commit is the last one on <forge>.
```

## Removal commit

Remove the box (and the empty line it leaves), nothing else.

```
Remove moved-to-GitHub note from README

The note is only meant for the archived <forge> repository. Keeping the
note and its removal in the history lets <forge> fast-forward to GitHub
if the project ever moves back.
```
