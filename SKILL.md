---
name: forgejo-to-github
description: Move a repository from Codeberg or another Forgejo instance to GitHub, including its issues, pull requests (as closed or open issues with the same numbers), labels, milestones, releases, description and topics. Creates the GitHub repository with gh (in the same-name GitHub org when there is one), pushes all branches and tags, points links to the repository in its files at GitHub, adds a moved-to-GitHub note on Codeberg (pushed to a review branch) whose commit and removal also land on GitHub so Codeberg can fast-forward later, and finishes with a todo list for the Codeberg web UI (description, website, archive). Use this whenever the user wants to migrate, move, transfer or "do the same as for dotfiles-nvim" for a codeberg.org or Forgejo/Gitea repository URL, mentions leaving Codeberg over its LLM / generative-AI Terms of Use, or asks to bring Forgejo issues and PRs over to GitHub, even if they only paste the repository URL.
---

# Move a Forgejo repository to GitHub

This repeats the migration of codeberg.org/stella10x1/dotfiles-nvim to
github.com/stella10x1/dotfiles-nvim, and of the lingfra org's repositories
to github.com/lingfra (November 2026). The script does the API work. You do
the git work and the README note, and the user does the Forgejo web UI steps
at the end, since Forgejo has no CLI.

**Tested with Forgejo `17.0.0-99-322c239+gitea-1.22.0`** (codeberg.org,
2026-11-10). The same version is `TESTED_FORGEJO_VERSION` in the script.

Files:
- `scripts/forgejo2github.py`: `inspect`, `create-repo`, `issues`,
  `milestones`, `releases`, `links`, `verify`. Everything but `inspect` and
  `verify` only writes with `--apply`. Run it with `python3 -I`.
- `tests/test_forgejo2github.py`: regression tests against archived
  reference repositories on Codeberg (read-only).
- `references/notice.md`: README note template, removal and commit messages.
  Read it before writing the note.

Input: at least the repository URL on the Forgejo instance (web, HTTPS or SSH
form). Optional: the GitHub target `OWNER/NAME` and former account names (see
Phase 1).

**Default target.** When the source belongs to an org and you are a member
of the same-name GitHub org who may create repositories there, the script
targets that org; otherwise your `gh` account, with the same repository
name. `inspect` prints which rule applied. Name the target in the Phase 3
confirmation either way, and pass `--github OWNER/NAME` explicitly in every
command once it is settled.

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
  the failures and fix the script before migrating. A 5xx from Codeberg is
  transient (the script retries a few times); run the tests again later.
  The reference repositories live on Codeberg. For other instances, the
  tests prove the script logic, and the dry run is the check for that
  instance.
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
python3 -I "$S" create-repo <url> --github OWNER/NAME
python3 -I "$S" issues <url> --github OWNER/NAME [--old-owner NAME ...] --dump-plan "$W/plan.json"
python3 -I "$S" milestones <url> --github OWNER/NAME [--old-owner NAME ...]
python3 -I "$S" releases <url> --github OWNER/NAME [--old-owner NAME ...]
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
- **Notes:** open PRs become open issues. If the user wants to keep an open
  PR's branch from a fork, push `refs/pull/N/head` to a GitHub branch later.
- **Placeholders** fill gaps from deleted issues, so the numbers stay
  aligned.
- `@name` mentions get wrapped in backticks, so unrelated GitHub users with
  the same name aren't notified.

`milestones` lists each milestone (state, due date, issues). GitHub keeps
only the due day. `releases` lists name, pre-release/draft flags and assets.
Release notes get the same link rewriting as issues. GitHub dates releases
on creation, so the original date is lost, and assets aren't migrated (the
dry run names them). With the releases unit disabled on the forge, it says
so and does nothing.

## Phase 2: Prepare git locally (no pushes yet)

```sh
git clone --mirror <forgejo-clone-url> "$W/repo.git"
git -C "$W/repo.git" remote remove origin
DEFAULT=<default_branch from inspect>
git -C "$W/repo.git" worktree add -b moved-to-github "$W/work" "$DEFAULT"
python3 -I "$S" links <url> --github OWNER/NAME [--old-owner NAME ...] --dir "$W/work"
```

Remove `origin` right after cloning. A mirror clone fetches with
`+refs/*:refs/*`, so every later push to the forge URL also moves the
local branch of the same name, as if it were a remote-tracking ref. The
Phase 4 push of the notice commit to `moved-to-github` then resets the
worktree branch to the notice commit while the files stay at the removal
commit, and the next commit silently contains the removal (this happened
on lingfra/ansible-collection-lingfra-shell). Deleting `moved-to-github`
on the forge in Phase 5 would delete the checked-out branch. All pushes
name the URL explicitly, so nothing needs `origin`.

`links` finds links to the repository in all tracked files and shows how
they would point at GitHub. That includes clone URLs, `.git` and SSH forms,
issues, PRs (as issues), `src/branch/...` (as `tree/...`), `raw/...`,
releases, compare, tags and commits. Paths without a GitHub counterpart
(wiki, archive, attachments) stay and are listed. Review the dry run:

- **Already dead on the forge** (checked through the API, since Codeberg
  answers unknown clients with a bot-check page and status 200): fix those
  by hand after `--apply`, e.g. a deleted branch → the default branch.
- **Links to other repositories of the same owner** (sibling repositories,
  submodules): leave them. List them in the report as candidates for a
  follow-up migration (Phase 5).
- **Generated URLs** (template code like `{{ ... }}`): other repositories'
  output may depend on them. Don't change them as part of the move. Ask the
  user, and prefer taking the URL from metadata (e.g. `galaxy.yml`'s
  `repository`) over hardcoding GitHub, as long as siblings still live on the
  forge.

Then run it with `--apply`, check `git -C "$W/work" diff`, rename link texts
that name the forge, and write the notice commit, then the removal commit,
following `references/notice.md`. The rewritten links go into the notice
commit. Remember the notice commit: `NOTICE=$(git -C "$W/work" rev-parse HEAD)`
right after committing it.

If a global pre-commit hook refuses because the repository has no
`.pre-commit-config.yaml`, commit with `PRE_COMMIT_ALLOW_NO_CONFIG=1`.
That's the hook's own opt-out, and there is nothing for it to check. If the
repository has a config, let the hooks run. If a hook changes files, review
them, then stage and commit again.

Check that `git -C "$W/work" log --oneline -3` shows removal → notice →
the original default branch head.

**Other cleanups that should reach the forge too** (e.g. an author rename,
more link fixes) belong between the original head and the notice commit,
before Phase 4. Ask the user about them in Phase 3. Once the forge is
archived, adding a commit there means unarchiving, rebasing the GitHub
default branch so the removal commit stays last, force-pushing both sides,
then archiving again and running `verify`. Changes only meant for GitHub can
simply go on top of the removal commit.

## Phase 3: Confirm once

Show the user what will be written and ask for one confirmation:
- the new GitHub repository (owner and why: org or account, visibility,
  description, topics),
- the pushes: all branches and tags to GitHub, the default branch with the
  extra commits, and `moved-to-github` (the notice commit) to Forgejo,
- the labels, the numbered list of issues and PRs, the milestones and the
  releases, from the dry runs,
- the files `links` rewrote, and what it left alone,
- the README note text,
- open questions: generated URLs, whether other cleanups should reach the
  forge before the note (see Phase 2).

## Phase 4: Apply, in this order

```sh
python3 -I "$S" create-repo <url> --github OWNER/NAME --apply
GH=git@github.com:OWNER/NAME.git
git -C "$W/repo.git" push "$GH" "refs/heads/*:refs/heads/*" "refs/tags/*:refs/tags/*" "^refs/heads/moved-to-github"
git -C "$W/work" push "$GH" "HEAD:refs/heads/${DEFAULT}"
gh repo edit OWNER/NAME --default-branch "$DEFAULT"
python3 -I "$S" issues <url> --github OWNER/NAME [--old-owner ...] --apply
python3 -I "$S" milestones <url> --github OWNER/NAME [--old-owner ...] --apply
python3 -I "$S" releases <url> --github OWNER/NAME [--old-owner ...] --apply
git -C "$W/work" push <forgejo-ssh-url> "${NOTICE}:refs/heads/moved-to-github"
```

- Push explicit refspecs. A plain `git push --mirror` would also send
  Forgejo's `refs/pull/*`, which GitHub rejects. `^refs/heads/...` is a
  negative refspec, so the worktree branch stays off GitHub. Don't write it
  as `:refs/...`, which would mean "delete". The second push of the default
  branch is a plain fast-forward.
- Write `${NOTICE}:` with braces. In zsh, `"$NOTICE:refs/..."` applies the
  `:r` modifier and mangles the refspec.
- `issues`, `milestones` and `releases` resume if interrupted. Issues
  already on GitHub must match the plan, and missing comments and closes are
  added. Milestones are matched by title, releases by tag. Just run them
  again. `milestones` needs the issues on GitHub, `releases` the tags.
- If GitHub assigns an unexpected number, the script stops. Don't work
  around that. Tell the user.

Spot-check afterwards: the issue count and states (`gh issue list -R ...
--state all`), labels, milestones, releases (`gh release list -R ...`), the
default branch log, and that
`gh api repos/OWNER/NAME/compare/$NOTICE...$DEFAULT --jq .status` says
`ahead`. Quote `gh api` paths that contain `?` (zsh globs them).

GitHub adds its default labels (`documentation`, `wontfix`, ...) when it
creates the repository. They stay next to the migrated ones. Mention them
in the report. If the user wants labels at org level: GitHub has no API for
an org's default labels (only the web UI, Org settings → Repository →
Repository defaults), and they're only copied into repositories created
afterwards. The migrated repository keeps its own labels, which its issues
use.

## Phase 5: Report and the user's todo list

Summarize what was migrated, anything not migrated (wiki; release assets and
the original release dates; reactions; review comments; attachments stay
hosted on Forgejo), and anything skipped. Also list:
- the links `links` left alone, grouped as it printed them, with the
  same-owner repositories as candidates for a follow-up migration. After
  migrating one, offer to update the repositories that point to it
  (submodule URL in `.gitmodules`, the submodule pin, links).
- if the working directory is a clone of the source: offer
  `git remote set-url origin git@github.com:OWNER/NAME.git` and
  `git pull --ff-only`.

Then give this list, filled in:

> Still to do on **<forge>** (in the web UI, in this order, since archiving
> makes the repository read-only):
>
> 0. Review the `moved-to-github` branch and merge it **fast-forward
>    only**: `git push <forgejo-ssh-url> <NOTICE sha>:refs/heads/<default>`,
>    or a pull request with the merge style "Fast-forward only". Not
>    "Rebase": that recreates the commit with a new hash, and Forgejo can't
>    be fast-forwarded to GitHub later. Then delete the branch. I can do
>    the push and the branch deletion for you once you've reviewed it.
> 1. Settings → Repository → Description:
>    `*** Moved to GitHub due to LLM restrictions ***`
> 2. Settings → Repository → Website: `https://github.com/OWNER/NAME`
> 3. Settings → Danger Zone → Archive this repository.
>
> Afterwards, `python3 -I ~/.claude/skills/forgejo-to-github/scripts/forgejo2github.py verify <url> --github OWNER/NAME`
> checks all four.

Step 0 doesn't need the web UI. When the user asks you to do it, push the
notice commit to the default branch (a fast-forward; if Git refuses, stop
and tell the user), then `git push <forgejo-ssh-url> --delete moved-to-github`.
Steps 1 to 3 are web UI only.

Without `--github`, `verify` takes the target from the website field once it
points at GitHub (step 2). Before that, it falls back to the default target,
which may be wrong. Always fill in `--github` in the todo list.

For a non-Codeberg instance, ask the user whether the description text fits
(the LLM restrictions are Codeberg's). `verify` compares against that exact
text.
