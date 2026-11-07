---
name: forgejo-to-github
description: Move a repository from Codeberg or another Forgejo instance to GitHub, including its issues, pull requests (as closed or open issues with the same numbers), labels, description and topics. Creates the GitHub repository with gh, pushes all branches and tags, adds a moved-to-GitHub note on Codeberg (pushed to a review branch) whose commit and removal also land on GitHub so Codeberg can fast-forward later, and finishes with a todo list for the Codeberg web UI (description, website, archive). Use this whenever the user wants to migrate, move, transfer or "do the same as for dotfiles-nvim" for a codeberg.org or Forgejo/Gitea repository URL, mentions leaving Codeberg over its LLM / generative-AI Terms of Use, or asks to bring Forgejo issues and PRs over to GitHub, even if they only paste the repository URL.
---

# Move a Forgejo repository to GitHub

This repeats the migration of codeberg.org/stella10x1/dotfiles-nvim to
github.com/stella10x1/dotfiles-nvim (November 2026). The script does the
API work. You do the git work and the README note, and the user does the
Forgejo web UI steps at the end, since Forgejo has no CLI.

**Tested with Forgejo `17.0.0-93-4d598f4+gitea-1.22.0`** (codeberg.org,
2026-11-07). The same version is `TESTED_FORGEJO_VERSION` in the script.

Files:
- `scripts/forgejo2github.py`: `inspect`, `create-repo`, `issues`, `verify`.
  `create-repo` and `issues` only write with `--apply`. Run it with
  `python3 -I`.
- `tests/test_forgejo2github.py`: regression test against the archived
  reference repository on Codeberg (read-only).
- `references/notice.md`: README note template, removal and commit messages.
  Read it before writing the note.

Input: at least the repository URL on the Forgejo instance (web, HTTPS or SSH
form). Optional: the GitHub target `OWNER/NAME` (default: the `gh` account,
same name) and former account names (see Phase 1).

GitHub issues can't be deleted, only closed. A wrong run leaves permanent
clutter, so everything is dry-run first and the user confirms once before
anything is written (Phase 3).

## Phase 0: Preconditions

```sh
S=~/.claude/skills/forgejo-to-github/scripts/forgejo2github.py
gh auth status
python3 -I "$S" inspect <url> [--github OWNER/NAME]
```

- **Forgejo version.** If `inspect` prints `CHANGED`, tell the user the
  instance now runs a different Forgejo version than the skill was tested
  with, and run the tests:
  ```sh
  python3 -I -m unittest discover -s ~/.claude/skills/forgejo-to-github/tests -v
  ```
  If they pass (the version test shows as skipped with the new version),
  the script still works as on the tested version. Tell the user that, and
  offer to bump the version in the script and in this file. Do that only if
  they agree, and run the tests again afterwards. If tests fail, stop: show
  the failures and fix the script before migrating. The reference
  repository lives on Codeberg. For other instances, the tests prove the
  script logic, and the dry run is the check for that instance.
- **Private repositories** need `FORGEJO_TOKEN` (Forgejo settings →
  Applications, read access to repository and issues) and an SSH clone URL.
- **GitHub target must not exist yet** (`inspect` says `EXISTS`). Issue
  numbers can only match on an empty tracker. Ask the user how to proceed
  instead of reusing the repo.
- **Archived source:** the notice branch can't be pushed. Ask the user to
  unarchive it first, or skip the Forgejo side of the note.
- **Description already the moved notice:** pass the real one with
  `create-repo --description`. Ask the user for it, or take it from the
  README.
- Push access to Forgejo over SSH: `git ls-remote <ssh-url>`.

## Phase 1: Dry runs

```sh
python3 -I "$S" create-repo <url> [--github OWNER/NAME]
python3 -I "$S" issues <url> [--github OWNER/NAME] [--old-owner NAME ...] --dump-plan "$W/plan.json"
```

Use a scratch directory as `$W` (the session scratchpad if there is one).

Review the `issues` output:
- **Links left on the instance:** the script points links into the same
  repository at GitHub (branches, commits, issues, PRs). It lists any other
  instance links it leaves alone. Check that they resolve
  (`curl -sI`). A link under another account name with the same repository
  name usually means the account was renamed. Re-run with
  `--old-owner <old-name>`. For example, `stella10x1` on Codeberg used to
  be `lingling`.
- **Notes:** milestones aren't migrated. Open PRs become open issues. If
  the user wants to keep an open PR's branch from a fork, push
  `refs/pull/N/head` to a GitHub branch later.
- **Placeholders** fill gaps from deleted issues, so the numbers stay
  aligned.
- `@name` mentions get wrapped in backticks, so unrelated GitHub users with
  the same name aren't notified.

## Phase 2: Prepare git locally (no pushes yet)

```sh
git clone --mirror <forgejo-clone-url> "$W/repo.git"
DEFAULT=<default_branch from inspect>
git -C "$W/repo.git" worktree add -b moved-to-github "$W/work" "$DEFAULT"
```

In `$W/work`, write the notice commit, then the removal commit, following
`references/notice.md`. Remember the notice commit:
`NOTICE=$(git -C "$W/work" rev-parse HEAD)` right after committing it.

If a global pre-commit hook refuses because the repository has no
`.pre-commit-config.yaml`, commit with `PRE_COMMIT_ALLOW_NO_CONFIG=1`.
That's the hook's own opt-out, and there is nothing for it to check. If the
repository has a config, let the hooks run. If a hook changes files, review
them, then stage and commit again.

Check that `git -C "$W/work" log --oneline -3` shows removal → notice →
the original default branch head.

## Phase 3: Confirm once

Show the user what will be written and ask for one confirmation:
- the new GitHub repository (visibility, description, topics),
- the pushes: all branches and tags to GitHub, the default branch with the
  two extra commits, and `moved-to-github` (the notice commit) to Forgejo,
- the labels and the numbered list of issues and PRs, from the dry run,
- the README note text.

## Phase 4: Apply, in this order

```sh
python3 -I "$S" create-repo <url> [--github ...] --apply
GH=git@github.com:OWNER/NAME.git
git -C "$W/repo.git" push "$GH" "refs/heads/*:refs/heads/*" "refs/tags/*:refs/tags/*" "^refs/heads/moved-to-github"
git -C "$W/work" push "$GH" "HEAD:refs/heads/${DEFAULT}"
gh repo edit OWNER/NAME --default-branch "$DEFAULT"
python3 -I "$S" issues <url> [--github ...] [--old-owner ...] --apply
git -C "$W/work" push <forgejo-ssh-url> "${NOTICE}:refs/heads/moved-to-github"
```

- Push explicit refspecs. A plain `git push --mirror` would also send
  Forgejo's `refs/pull/*`, which GitHub rejects. `^refs/heads/...` is a
  negative refspec, so the worktree branch stays off GitHub. Don't write it
  as `:refs/...`, which would mean "delete". The second push of the default
  branch is a plain fast-forward.
- Write `${NOTICE}:` with braces. In zsh, `"$NOTICE:refs/..."` applies the
  `:r` modifier and mangles the refspec.
- `issues --apply` resumes if it was interrupted: issues already on GitHub
  must match the plan, missing comments and closes are added. Just run it
  again.
- If GitHub assigns an unexpected number, the script stops. Don't work
  around that. Tell the user.

Spot-check afterwards: the issue count and states (`gh issue list -R ...
--state all`), labels, the default branch log, and that
`gh api repos/OWNER/NAME/compare/$NOTICE...$DEFAULT --jq .status` says
`ahead`.

## Phase 5: Report and the user's todo list

Summarize what was migrated, anything not migrated (releases: tags are
there, but release notes and assets are not; wiki; milestones; reactions;
review comments; attachments stay hosted on Forgejo), and anything skipped.
Then give this list, filled in:

> Still to do on **<forge>** (in the web UI, in this order, since archiving
> makes the repository read-only):
>
> 0. Review the `moved-to-github` branch and merge it **fast-forward
>    only**: `git push <forgejo-ssh-url> <NOTICE sha>:refs/heads/<default>`,
>    or a pull request with the merge style "Fast-forward only". Not
>    "Rebase": that recreates the commit with a new hash, and Forgejo can't
>    be fast-forwarded to GitHub later. Then delete the branch.
> 1. Settings → Repository → Description:
>    `*** Moved to GitHub due to LLM restrictions ***`
> 2. Settings → Repository → Website: `https://github.com/OWNER/NAME`
> 3. Settings → Danger Zone → Archive this repository.
>
> Afterwards, `python3 -I ~/.claude/skills/forgejo-to-github/scripts/forgejo2github.py verify <url>`
> checks all four.

For a non-Codeberg instance, ask the user whether the description text fits
(the LLM restrictions are Codeberg's). `verify` compares against that exact
text.
