#!/usr/bin/env python3
"""Move a Forgejo (e.g. Codeberg) repository's metadata, labels and issues to GitHub.

Subcommands (every write is a dry run unless --apply is given):
  inspect      show the source repository, the Forgejo version and the GitHub target
  create-repo  create the GitHub repository with the source description and topics
  issues       copy labels, then issues and pull requests (as issues) in number order
  verify       check the manual follow-up steps on the Forgejo side

Uses only the Python standard library and the gh CLI. Set FORGEJO_TOKEN for
private repositories.
"""
import argparse
import json
import os
import re
import subprocess
import sys
import time
import urllib.parse
import urllib.request

# Forgejo version this script was written and tested against (codeberg.org, 2026-11-07).
# Keep in sync with SKILL.md; the test suite checks that.
TESTED_FORGEJO_VERSION = "17.0.0-93-4d598f4+gitea-1.22.0"
MOVED_DESCRIPTION = "*** Moved to GitHub due to LLM restrictions ***"
PAGE = 50


class Source:
    """A repository on a Forgejo instance, addressed by any of its URLs."""

    def __init__(self, url, token=None):
        m = re.match(r"^https?://(?P<host>[^/]+)/(?P<owner>[^/]+)/(?P<repo>[^/#?]+?)(?:\.git)?(?:[/#?].*)?$", url) or re.match(
            r"^(?:ssh://)?[^@/]+@(?P<host>[^:/]+)(?::\d+)?[:/](?P<owner>[^/]+)/(?P<repo>[^/]+?)(?:\.git)?/?$", url
        )
        if not m:
            sys.exit(f"not a Forgejo repository URL: {url}")
        self.host, self.owner, self.repo = m["host"], m["owner"], m["repo"]
        self.full = f"{self.owner}/{self.repo}"
        self.web = f"https://{self.host}/{self.full}"
        self.api_base = f"https://{self.host}/api/v1"
        self.forge = "Codeberg" if self.host == "codeberg.org" else self.host
        self.token = token

    def api(self, path, params=None):
        url = f"{self.api_base}/{path}"
        if params:
            url += "?" + urllib.parse.urlencode(params)
        headers = {"Accept": "application/json"}
        if self.token:
            headers["Authorization"] = f"token {self.token}"
        with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=30) as r:
            return json.load(r)

    def paged(self, path, **params):
        # Stops on the first page without new ids, so endpoints that ignore
        # page/limit (and return everything at once) don't loop forever.
        seen, out, page = set(), [], 1
        while True:
            items = self.api(path, {**params, "limit": PAGE, "page": page})
            new = [i for i in items if i["id"] not in seen]
            if not new:
                return out
            seen.update(i["id"] for i in new)
            out += new
            if len(items) < PAGE:
                return out
            page += 1

    def version(self):
        return self.api("version")["version"]


def gh(args, payload=None, retries=5) -> str:
    for attempt in range(retries):
        p = subprocess.run(
            ["gh", *args], input=json.dumps(payload) if payload is not None else None, capture_output=True, text=True
        )
        if p.returncode == 0:
            return p.stdout
        if "rate limit" in p.stderr.lower() and attempt < retries - 1:
            wait = 60 * (attempt + 1)
            print(f"  GitHub rate limit, waiting {wait}s", file=sys.stderr)
            time.sleep(wait)
            continue
        sys.exit(f"gh {' '.join(args)} failed:\n{p.stderr}")
    raise AssertionError("unreachable")


def gh_api(path, method="GET", payload=None):
    args = ["api", path, "-X", method]
    if payload is not None:
        args += ["--input", "-"]
    out = gh(args, payload)
    if method != "GET":
        time.sleep(1)  # stay clear of GitHub's secondary rate limit for content creation
    return json.loads(out) if out.strip() else None


def gh_repo_exists(gh_repo):
    return subprocess.run(["gh", "repo", "view", gh_repo], capture_output=True).returncode == 0


def default_target(src):
    return f"{gh(['api', 'user', '--jq', '.login']).strip()}/{src.repo}"


def date(ts):
    return ts[:10]


def rewrite_links(text, src, gh_repo, old_owners=()):
    """Point links into the source repository at the GitHub copy."""
    host = re.escape(src.host)
    # Relative links (e.g. uploaded attachments) only resolve on the Forgejo instance.
    text = re.sub(r"(\]\(|src=\")/(?!/)", rf"\1https://{src.host}/", text)
    owners = "|".join(re.escape(o) for o in (src.owner, *old_owners))
    base = rf"https?://{host}/(?:{owners})/{re.escape(src.repo)}"
    gh_url = f"https://github.com/{gh_repo}"
    text = re.sub(base + r"/src/(?:branch|tag|commit)/", gh_url + "/tree/", text)
    text = re.sub(base + r"/(?:issues|pulls)/(\d+)", gh_url + r"/issues/\1", text)
    text = re.sub(base + r"/commit/", gh_url + "/commit/", text)
    # Any other link to a renamed account 404s; it lives under the current name now.
    for o in old_owners:
        text = re.sub(rf"(https?://{host}/){re.escape(o)}/", rf"\g<1>{src.owner}/", text)
    return text


def neutralize_mentions(text):
    """Wrap @name in backticks: a Forgejo user name may belong to an unrelated GitHub user."""
    out, fence = [], False
    for line in text.split("\n"):
        if line.lstrip().startswith(("```", "~~~")):
            fence = not fence
        elif not fence:
            parts = line.split("`")
            for i in range(0, len(parts), 2):  # even parts are outside inline code
                parts[i] = re.sub(r"(?<![\w/@])@([A-Za-z0-9][A-Za-z0-9_.-]*[A-Za-z0-9]|[A-Za-z0-9])", r"`@\1`", parts[i])
            line = "`".join(parts)
        out.append(line)
    return "\n".join(out)


def by(src, user):
    login = user["login"] if user else "ghost"
    return "" if login == src.owner else f" by [{login}](https://{src.host}/{login})"


def fix(text, src, gh_repo, old_owners):
    return neutralize_mentions(rewrite_links(text or "", src, gh_repo, old_owners))


def plan_labels(src, issues):
    labels = {}
    # Issues can carry org-level labels that aren't in the repository's own list.
    for lbl in [*src.paged(f"repos/{src.full}/labels"), *(lbl for it in issues for lbl in it["labels"])]:
        labels.setdefault(
            lbl["name"], {"name": lbl["name"], "color": lbl["color"].lstrip("#"), "description": (lbl.get("description") or "")[:100]}
        )
    return list(labels.values())


def plan_issues(src, gh_repo, old_owners=()):
    """Everything to create on GitHub, in number order, without touching GitHub."""
    issues = sorted(src.paged(f"repos/{src.full}/issues", state="all"), key=lambda i: i["number"])
    by_number = {it["number"]: it for it in issues}
    plan = []
    for n in range(1, (issues[-1]["number"] if issues else 0) + 1):
        it = by_number.get(n)
        if it is None:
            plan.append(
                {
                    "number": n,
                    "kind": "placeholder",
                    "title": f"(deleted {src.forge} issue #{n})",
                    "body": f"> Placeholder: #{n} no longer exists on {src.forge}. It keeps the issue numbers in sync.",
                    "labels": [],
                    "comments": [],
                    "state": "closed",
                    "reason": "not_planned",
                    "notes": [],
                }
            )
            continue
        notes = []
        opened = f", opened{by(src, it['user'])} on {date(it['created_at'])}"
        if it.get("pull_request"):
            kind = "pull"
            pr = src.api(f"repos/{src.full}/pulls/{n}")
            head, base = pr["head"], pr["base"]["ref"]
            if head["ref"].startswith("refs/"):  # source branch was deleted
                branches = f" into `{base}`"
            elif head.get("repo") and head["repo"]["full_name"] != src.full:
                branches = f" (`{head['repo']['full_name']}:{head['ref']}` → `{base}`)"
            else:
                branches = f" (`{head['ref']}` → `{base}`)"
            if pr["merged"]:
                outcome, reason = f", merged on {date(pr['merged_at'])}", "completed"
            elif it["state"] == "closed":
                outcome, reason = f", closed without merging on {date(pr['closed_at'])}", "not_planned"
            else:
                outcome, reason = ", still open when it was migrated", None
                notes.append(f"open pull request, head {head['ref']!r}")
            header = f"> Migrated from {src.forge} pull request [{src.full}#{n}]({src.web}/pulls/{n}){branches}{opened}{outcome}."
        else:
            kind, reason = "issue", "completed" if it["state"] == "closed" else None
            header = f"> Migrated from {src.forge} issue [{src.full}#{n}]({src.web}/issues/{n}){opened}."
        if it.get("milestone"):
            notes.append(f"milestone {it['milestone']['title']!r} is not migrated")
        body = it["body"] or ""
        comments = [
            f"> Comment migrated from [{src.forge}]({c['html_url']}), posted{by(src, c['user'])} on {date(c['created_at'])}."
            f"\n\n{fix(c['body'], src, gh_repo, old_owners)}"
            for c in (src.paged(f"repos/{src.full}/issues/{n}/comments") if it["comments"] else [])
        ]
        plan.append(
            {
                "number": n,
                "kind": kind,
                "title": it["title"],
                "body": header + ("\n\n" + fix(body, src, gh_repo, old_owners) if body.strip() else ""),
                "labels": [lbl["name"] for lbl in it["labels"]],
                "comments": comments,
                "state": it["state"],
                "reason": reason,
                "notes": notes,
            }
        )
    return issues, plan


def remaining_links(plan, src):
    """Links into the Forgejo instance that survive the rewrite, for a human to check."""
    links = set()
    for item in plan:
        for text in (item["body"], *item["comments"]):
            for line in text.split("\n"):
                if line.startswith("> Migrated from") or line.startswith("> Comment migrated from"):
                    continue
                links.update(re.findall(rf"https?://{re.escape(src.host)}/[^\s)\]>\"']+", line))
    return sorted(links)


def gh_existing_issues(gh_repo):
    out = gh(
        ["api", "--paginate", f"repos/{gh_repo}/issues?state=all&per_page=100", "--jq",
         ".[] | {number, title, comments, state, pr: (.pull_request != null)}"]
    )
    return {i["number"]: i for i in map(json.loads, out.splitlines())}


def cmd_inspect(args, src, gh_repo):
    version = src.version()
    meta = src.api(f"repos/{src.full}")
    topics = src.api(f"repos/{src.full}/topics", {"limit": PAGE})["topics"]
    print(f"source:          {src.web} ({src.forge})")
    print(f"forgejo version: {version}")
    if version == TESTED_FORGEJO_VERSION:
        print("                 matches the tested version")
    else:
        print(f"                 CHANGED: tested with {TESTED_FORGEJO_VERSION}; run the test suite before migrating")
    for key in ("description", "website", "private", "archived", "default_branch", "has_issues", "has_wiki", "mirror"):
        print(f"{key + ':':<17}{meta.get(key)!r}")
    print(f"{'topics:':<17}{topics}")
    print(f"github target:   https://github.com/{gh_repo} ({'EXISTS' if gh_repo_exists(gh_repo) else 'does not exist yet'})")
    if meta.get("description") == MOVED_DESCRIPTION:
        print("note: the description already is the moved notice; pass --description to create-repo")
    if meta.get("archived"):
        print("note: the repository is archived, so the notice branch can't be pushed until it is unarchived")


def sanitize_topic(t):
    return re.sub(r"[^a-z0-9-]+", "-", t.lower()).strip("-")[:50]


def cmd_create_repo(args, src, gh_repo):
    meta = src.api(f"repos/{src.full}")
    topics = src.api(f"repos/{src.full}/topics", {"limit": PAGE})["topics"]
    description = args.description if args.description is not None else (meta.get("description") or "")
    if args.description is None and description == MOVED_DESCRIPTION:
        sys.exit("the source description is already the moved notice; pass --description with the real one")
    description = " ".join(description.split())[:350]
    gh_topics = [t for t in dict.fromkeys(map(sanitize_topic, topics)) if t]
    create = ["repo", "create", gh_repo, "--private" if meta["private"] else "--public"]
    if description:
        create += ["--description", description]
    if not meta.get("has_issues", True):
        create.append("--disable-issues")
    if not meta.get("has_wiki", True):
        create.append("--disable-wiki")
    print(f"create:  https://github.com/{gh_repo} ({'private' if meta['private'] else 'public'})")
    print(f"description: {description!r}")
    print(f"topics:  {gh_topics}" + (f" (from {topics})" if gh_topics != topics else ""))
    exists = gh_repo_exists(gh_repo)
    if exists:
        print("note: the GitHub repository already exists")
    if not args.apply:
        print("\nDry run only. Re-run with --apply to create it.")
        return
    if exists:
        sys.exit(f"{gh_repo} already exists on GitHub; not touching it")
    gh(create)
    if gh_topics:
        gh(["repo", "edit", gh_repo, "--add-topic", ",".join(gh_topics)])
    print(f"created https://github.com/{gh_repo}")


def cmd_issues(args, src, gh_repo):
    issues, plan = plan_issues(src, gh_repo, args.old_owner)
    labels = plan_labels(src, issues)
    if args.dump_plan:
        with open(args.dump_plan, "w") as f:
            json.dump({"labels": labels, "issues": plan}, f, indent=2, ensure_ascii=False)
        print(f"plan written to {args.dump_plan}")
    for lbl in labels:
        print(f"label: {lbl['name']} #{lbl['color']} {lbl['description']!r}")
    for item in plan:
        state = item["state"] + (f"/{item['reason']}" if item["state"] == "closed" else "")
        kind = {"issue": "issue", "pull": "PR→issue", "placeholder": "placeholder"}[item["kind"]]
        print(f"#{item['number']} {kind} [{state}] {item['title']!r} labels={item['labels']} comments={len(item['comments'])}")
        for note in item["notes"]:
            print(f"    note: {note}")
    links = remaining_links(plan, src)
    if links:
        print(f"\nlinks to {src.host} left as they are (check that they still resolve):")
        for link in links:
            print(f"  {link}")
    if not args.apply:
        print("\nDry run only. Re-run with --apply to create these on GitHub.")
        return

    # Re-running resumes: issues already on GitHub must match the plan in order.
    existing = gh_existing_issues(gh_repo)
    for n, e in sorted(existing.items()):
        if e["pr"] or n > len(plan) or plan[n - 1]["title"] != e["title"]:
            sys.exit(f"GitHub #{n} {e['title']!r} doesn't match the plan; refusing to continue")
    for lbl in labels:
        gh(["label", "create", lbl["name"], "-R", gh_repo, "--force", "--color", lbl["color"], "--description", lbl["description"]])
    for item in plan:
        n, e = item["number"], existing.get(item["number"])
        if e is None:
            created = gh_api(
                f"repos/{gh_repo}/issues", "POST", {"title": item["title"], "body": item["body"], "labels": item["labels"]}
            )
            assert created is not None
            if created["number"] != n:
                sys.exit(f"GitHub assigned #{created['number']} instead of #{n}; stopping")
            done, state = 0, "open"
        else:
            done, state = e["comments"], e["state"]
        for body in item["comments"][done:]:
            gh_api(f"repos/{gh_repo}/issues/{n}/comments", "POST", {"body": body})
        if item["state"] == "closed" and state != "closed":
            gh_api(f"repos/{gh_repo}/issues/{n}", "PATCH", {"state": "closed", "state_reason": item["reason"]})
        print(f"#{n} done")


def cmd_verify(args, src, gh_repo):
    meta = src.api(f"repos/{src.full}")
    gh_url = f"https://github.com/{gh_repo}"
    gh_default = gh(["api", f"repos/{gh_repo}", "--jq", ".default_branch"]).strip()
    head = src.api(f"repos/{src.full}/branches/{meta['default_branch']}")["commit"]["id"]
    p = subprocess.run(
        ["gh", "api", f"repos/{gh_repo}/compare/{head}...{gh_default}", "--jq", ".status"], capture_output=True, text=True
    )
    ff = p.returncode == 0 and p.stdout.strip() in ("ahead", "identical")
    checks = [
        (f"{src.forge} {meta['default_branch']} ({head[:7]}) is in GitHub's {gh_default} history (fast-forward possible)", ff),
        (f"description is {MOVED_DESCRIPTION!r}", meta.get("description") == MOVED_DESCRIPTION),
        (f"website is {gh_url}", (meta.get("website") or "").rstrip("/") == gh_url),
        ("repository is archived", bool(meta.get("archived"))),
    ]
    for text, ok in checks:
        print(f"{'✓' if ok else '✗'} {text}")
    if not ff:
        print("  The notice must reach the default branch as the very commit that was pushed (fast-forward only);")
        print("  a rebase or merge commit creates a different commit that GitHub's history doesn't contain.")
    sys.exit(0 if all(ok for _, ok in checks) else 1)


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    for name in ("inspect", "create-repo", "issues", "verify"):
        s = sub.add_parser(name)
        s.add_argument("url", help="repository URL on the Forgejo instance (web, HTTPS or SSH)")
        s.add_argument("--github", metavar="OWNER/NAME", help="target repository (default: your gh account, same name)")
        if name in ("create-repo", "issues"):
            s.add_argument("--apply", action="store_true", help="write to GitHub (default: dry run)")
        if name == "create-repo":
            s.add_argument("--description", help="use this instead of the source description")
        if name == "issues":
            s.add_argument("--old-owner", action="append", default=[], metavar="NAME",
                           help="former name of the owner account; links using it are rewritten (repeatable)")
            s.add_argument("--dump-plan", metavar="FILE", help="write the full plan as JSON for review")
    args = p.parse_args(argv)
    src = Source(args.url, os.environ.get("FORGEJO_TOKEN"))
    gh_repo = args.github or default_target(src)
    {"inspect": cmd_inspect, "create-repo": cmd_create_repo, "issues": cmd_issues, "verify": cmd_verify}[args.cmd](args, src, gh_repo)


if __name__ == "__main__":
    main()
