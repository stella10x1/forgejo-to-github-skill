#!/usr/bin/env python3
"""Move a Forgejo (e.g. Codeberg) repository's metadata, issues, milestones, releases and links to GitHub.

Subcommands (every write is a dry run unless --apply is given):
  inspect      show the source repository, the Forgejo version and the GitHub target
  create-repo  create the GitHub repository with the source description and topics
  issues       copy labels, then issues and pull requests (as issues) in number order
  milestones   copy milestones and assign them to the migrated issues (after issues)
  releases     copy release names, notes and flags onto the pushed tags
  links        point links to the repository in its own files (a worktree) at GitHub
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
import urllib.error
import urllib.parse
import urllib.request

# Forgejo version the test suite last passed against (codeberg.org, 2026-11-10; written against
# 17.0.0-93-4d598f4+gitea-1.22.0 on 2026-11-07).
# Keep in sync with SKILL.md; the test suite checks that.
TESTED_FORGEJO_VERSION = "17.0.0-99-322c239+gitea-1.22.0"
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
        for attempt in range(3):
            try:
                with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=30) as r:
                    return json.load(r)
            except urllib.error.HTTPError as e:
                # Codeberg sometimes answers 502/503/504 under load.
                if e.code < 500 or attempt == 2:
                    raise
                time.sleep(10 * (attempt + 1))
        raise AssertionError("unreachable")

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

    def is_org(self):
        return self.found(f"orgs/{self.owner}")

    def found(self, path, params=None):
        try:
            self.api(path, params)
            return True
        except urllib.error.HTTPError as e:
            if e.code == 404:
                e.close()
                return False
            raise

    def link_exists(self, url):
        """Whether a web link into this repository resolves, checked through the API, or None if it can't tell.

        The web UI can't be asked directly: Codeberg answers unknown clients with
        a bot-check page and status 200, even for missing pages.
        """
        m = re.match(rf"^https?://{re.escape(self.host)}/{re.escape(self.full)}/(.+?)/?(?:[#?].*)?$", url)
        if not m:
            return None
        rest = urllib.parse.unquote(m[1])
        r = f"repos/{self.full}"
        if mm := re.match(r"^(issues|pulls)/(\d+)$", rest):
            return self.found(f"{r}/issues/{mm[2]}")
        if mm := re.match(r"^commit/([0-9a-f]{7,40})$", rest):
            return self.found(f"{r}/git/commits/{mm[1]}")
        if mm := re.match(r"^releases/tag/(.+)$", rest):
            return self.found(f"{r}/releases/tags/{urllib.parse.quote(mm[1], safe='')}")
        if mm := re.match(r"^(?:src|raw)/(branch|tag|commit)/(.+)$", rest):
            kind, parts = mm[1], mm[2].split("/")
            # Branch and tag names may contain slashes: try the shortest existing ref first, like Forgejo does.
            for i in range(1, len(parts) + 1):
                ref, path = "/".join(parts[:i]), "/".join(parts[i:])
                if kind == "commit" or self.found(f"{r}/{'branches' if kind == 'branch' else 'tags'}/{urllib.parse.quote(ref, safe='')}"):
                    if not path:
                        return kind != "commit" or self.found(f"{r}/git/commits/{ref}")
                    return self.found(f"{r}/contents/{urllib.parse.quote(path)}", {"ref": ref})
            return False
        return None


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
    """The same-name GitHub org if the source belongs to an org and you may create repositories there, else your account."""
    if src.is_org():
        p = subprocess.run(["gh", "api", f"user/memberships/orgs/{src.owner}"], capture_output=True, text=True)
        if p.returncode == 0:
            m = json.loads(p.stdout)
            can_create = m.get("role") == "admin" or gh(
                ["api", f"orgs/{src.owner}", "--jq", ".members_can_create_repositories"]
            ).strip() == "true"
            if m.get("state") == "active" and can_create:
                return f"{src.owner}/{src.repo}", f"same-name GitHub org, you are {m['role']} there"
    return f"{gh(['api', 'user', '--jq', '.login']).strip()}/{src.repo}", "your gh account"


def website_target(website):
    """OWNER/NAME if the website field points at a GitHub repository (as it does after the move)."""
    m = re.match(r"^https://github\.com/([\w.-]+/[\w.-]+?)(?:\.git)?/?$", website or "")
    return m[1] if m else None


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


# Where a URL ends in prose, Markdown, YAML or a .gitmodules file.
URL_END = r"(?=\s|[)\]>\"'`,;]|\.(?!\w)|\Z)"
# Paths under the repository that GitHub serves the same way. Others (wiki, archive, ...) stay for review.
SAME_PATHS = r"issues|pulls|commit|compare|releases|tags|branches"


def rewrite_file_links(text, src, gh_repo, old_owners=()):
    """Point links to the source repository in its own files at GitHub.

    Unlike rewrite_links (for issue texts), relative links stay as they are: in
    the repository's files they point into the repository itself.
    """
    host, repo = re.escape(src.host), re.escape(src.repo)
    owners = "|".join(re.escape(o) for o in (src.owner, *old_owners))
    gh_url = f"https://github.com/{gh_repo}"
    text = re.sub(rf"git@{host}:(?:{owners})/{repo}\.git{URL_END}", f"git@github.com:{gh_repo}.git", text)
    text = re.sub(rf"ssh://git@{host}(?::\d+)?/(?:{owners})/{repo}\.git{URL_END}", f"ssh://git@github.com/{gh_repo}.git", text)
    base = rf"https?://{host}/(?:{owners})/{repo}"
    text = re.sub(base + r"/src/(?:branch|tag|commit)/", gh_url + "/tree/", text)
    text = re.sub(base + r"/raw/(?:branch|tag|commit)/", gh_url + "/raw/", text)
    text = re.sub(base + r"/pulls/(\d+)", gh_url + r"/issues/\1", text)  # pull requests became issues
    text = re.sub(base + rf"(?=(?:\.git|/)?{URL_END}|/(?:{SAME_PATHS})(?![\w-]))", gh_url, text)
    for o in old_owners:
        text = re.sub(rf"(https?://{host}/){re.escape(o)}/", rf"\g<1>{src.owner}/", text)
    return text


def instance_urls(text, src):
    pattern = rf"(?:https?://|ssh://git@|git@){re.escape(src.host)}(?::\d+)?[:/][^\s)\]>\"'`]+"
    return [u.rstrip(".,;:") for u in re.findall(pattern, text)]


def url_repo(url, src):
    """OWNER/NAME of a URL on the instance, or None for instance pages like /user/login."""
    m = re.match(rf"^(?:https?://|ssh://git@|git@){re.escape(src.host)}(?::\d+)?[:/]([^/]+)/([^/#?]+?)(?:\.git)?(?:[/#?]|$)", url)
    return f"{m[1]}/{m[2]}" if m else None


def plan_file_links(files, src, gh_repo, old_owners=()):
    """For {path: text}: the rewritten texts, each rewritten URL, and the instance URLs left alone."""
    new_texts, changed, left = {}, [], []
    for path, text in sorted(files.items()):
        new = rewrite_file_links(text, src, gh_repo, old_owners)
        if new != text:
            new_texts[path] = new
        for n, (old_line, new_line) in enumerate(zip(text.split("\n"), new.split("\n")), 1):
            for url in instance_urls(old_line, src):
                rewritten = rewrite_file_links(url, src, gh_repo, old_owners)
                if rewritten != url:
                    changed.append((path, n, url, rewritten))
            left += [(path, n, url) for url in instance_urls(new_line, src)]
    return new_texts, changed, left


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
            notes.append(f"milestone {it['milestone']['title']!r} is assigned by the milestones subcommand")
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


def plan_milestones(src, gh_repo, old_owners=()):
    if not src.api(f"repos/{src.full}").get("has_issues", True):
        return []
    issues = src.paged(f"repos/{src.full}/issues", state="all")
    plan = []
    for m in sorted(src.paged(f"repos/{src.full}/milestones", state="all"), key=lambda m: m["id"]):
        plan.append(
            {
                "title": m["title"],
                "description": fix(m.get("description"), src, gh_repo, old_owners),
                "state": m["state"],
                # GitHub keeps only the day; noon UTC keeps it from shifting a day in any time zone.
                "due_on": f"{date(m['due_on'])}T12:00:00Z" if m.get("due_on") else None,
                "issues": sorted(i["number"] for i in issues if (i.get("milestone") or {}).get("id") == m["id"]),
            }
        )
    return plan


def plan_releases(src, gh_repo, old_owners=()):
    # With the releases unit disabled, the endpoint answers 404 "The target couldn't be found."
    if not src.api(f"repos/{src.full}").get("has_releases", True):
        return []
    plan = []
    for r in sorted(src.paged(f"repos/{src.full}/releases"), key=lambda r: r["created_at"]):
        plan.append(
            {
                "tag": r["tag_name"],
                "name": r["name"] or r["tag_name"],
                "body": fix((r["body"] or "").replace("\r\n", "\n"), src, gh_repo, old_owners),
                "draft": r["draft"],
                "prerelease": r["prerelease"],
                "published": date(r["published_at"] or r["created_at"]),
                "assets": [a["name"] for a in r.get("assets") or []],
            }
        )
    return plan


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
         ".[] | {number, title, comments, state, pr: (.pull_request != null), milestone: .milestone.title}"]
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
    for key in ("description", "website", "private", "archived", "default_branch", "has_issues", "has_wiki", "has_releases", "mirror"):
        print(f"{key + ':':<17}{meta.get(key)!r}")
    print(f"{'topics:':<17}{topics}")
    milestones = src.paged(f"repos/{src.full}/milestones", state="all") if meta.get("has_issues", True) else []
    releases = src.paged(f"repos/{src.full}/releases") if meta.get("has_releases", True) else []
    print(f"{'milestones:':<17}{len(milestones)}")
    print(f"{'releases:':<17}{len(releases)}")
    exists = "EXISTS" if gh_repo_exists(gh_repo) else "does not exist yet"
    print(f"github target:   https://github.com/{gh_repo} ({exists}{'; ' + args.target_reason if args.target_reason else ''})")
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


def cmd_milestones(args, src, gh_repo):
    plan = plan_milestones(src, gh_repo, args.old_owner)
    for m in plan:
        due = f" due {m['due_on'][:10]}" if m["due_on"] else ""
        print(f"milestone: {m['title']!r} [{m['state']}]{due} issues={['#%d' % n for n in m['issues']]}")
    if not plan:
        print("no milestones")
        return
    if not args.apply:
        print("\nDry run only. Re-run with --apply (after issues --apply) to create these on GitHub.")
        return

    # Re-running resumes: milestones are matched by title, assignments are only changed where they differ.
    existing = gh_existing_issues(gh_repo)
    missing = sorted({n for m in plan for n in m["issues"]} - existing.keys())
    if missing:
        sys.exit(f"issues {missing} aren't on GitHub yet; run issues --apply first")
    out = gh(["api", "--paginate", f"repos/{gh_repo}/milestones?state=all&per_page=100", "--jq", ".[] | {title, number}"])
    numbers = {m["title"]: m["number"] for m in map(json.loads, out.splitlines())}
    for m in plan:
        if m["title"] not in numbers:
            payload = {"title": m["title"], "state": m["state"], "description": m["description"]}
            if m["due_on"]:
                payload["due_on"] = m["due_on"]
            created = gh_api(f"repos/{gh_repo}/milestones", "POST", payload)
            assert created is not None
            numbers[m["title"]] = created["number"]
        for n in m["issues"]:
            if existing[n]["milestone"] != m["title"]:
                gh_api(f"repos/{gh_repo}/issues/{n}", "PATCH", {"milestone": numbers[m["title"]]})
        print(f"milestone {m['title']!r} done")


def gh_tag_exists(gh_repo, tag):
    ref = urllib.parse.quote(tag, safe="")
    return subprocess.run(["gh", "api", f"repos/{gh_repo}/git/ref/tags/{ref}"], capture_output=True).returncode == 0


def cmd_releases(args, src, gh_repo):
    plan = plan_releases(src, gh_repo, args.old_owner)
    for r in plan:
        flags = "".join(f" {f}" for f, on in (("draft", r["draft"]), ("pre-release", r["prerelease"])) if on)
        print(f"release: {r['tag']} {r['name']!r}{flags}, published {r['published']}")
        if r["assets"]:
            print(f"    note: {len(r['assets'])} assets aren't migrated ({', '.join(r['assets'])}): {src.web}/releases/tag/{r['tag']}")
    if not plan:
        print("no releases" + ("" if src.api(f"repos/{src.full}").get("has_releases", True) else " (releases are disabled)"))
        return
    print("note: GitHub dates the releases on creation; the original dates aren't kept")
    if not args.apply:
        print("\nDry run only. Re-run with --apply (after pushing the tags) to create these on GitHub.")
        return

    # Re-running resumes: releases already on GitHub (by tag) are skipped. Oldest first, so the newest stays "latest".
    out = gh(["api", "--paginate", f"repos/{gh_repo}/releases?per_page=100", "--jq", ".[].tag_name"])
    existing = set(out.split())
    for r in plan:
        if r["tag"] in existing:
            print(f"{r['tag']}: already on GitHub")
            continue
        if not gh_tag_exists(gh_repo, r["tag"]):
            if r["draft"]:
                print(f"{r['tag']}: draft without a tag on GitHub, skipped")
                continue
            sys.exit(f"tag {r['tag']} isn't on GitHub; push the tags first")
        gh_api(
            f"repos/{gh_repo}/releases",
            "POST",
            {
                "tag_name": r["tag"],
                "name": r["name"],
                "body": r["body"],
                "draft": r["draft"],
                "prerelease": r["prerelease"],
                "make_latest": "false" if r["draft"] or r["prerelease"] else "true",
            },
        )
        print(f"{r['tag']} done")


def read_worktree(path):
    """{path: text} of the tracked text files in a git worktree (submodules and binary files skipped)."""
    out = subprocess.run(["git", "-C", path, "ls-files", "-z"], capture_output=True, text=True, check=True).stdout
    files = {}
    for name in filter(None, out.split("\0")):
        full = os.path.join(path, name)
        if not os.path.isfile(full) or os.path.islink(full):
            continue
        try:
            with open(full, encoding="utf-8", newline="") as f:
                text = f.read()
        except UnicodeDecodeError:
            continue
        if "\0" not in text:
            files[name] = text
    return files


def cmd_links(args, src, gh_repo):
    new_texts, changed, left = plan_file_links(read_worktree(args.dir), src, gh_repo, args.old_owner)
    for path, n, old, new in changed:
        print(f"{path}:{n}: {old} -> {new}")
    if not changed:
        print(f"no links to {src.full} in the files")
    dead = sorted({old for _, _, old, _ in changed if src.link_exists(old) is False})
    if dead:
        print(f"\nalready dead on {src.forge} (rewritten too, but fix them by hand):")
        for url in dead:
            print(f"  {url}")
    groups = {}
    for path, n, url in left:
        if re.search(r"\{\{|\{%|\$\{", url):
            key = "(generated)"
        else:
            key = url_repo(url, src) or "(accounts and other pages)"
        groups.setdefault(key, []).append(f"{path}:{n} {url}")
    if groups:
        print(f"\nlinks to {src.host} left as they are:")
        for repo, where in sorted(groups.items()):
            if repo == "(generated)":
                kind = "template code builds these; other repositories' output may depend on it"
            elif repo == "(accounts and other pages)":
                kind = "not a repository"
            elif repo == src.full:
                kind = "this repository, a path without a GitHub counterpart"
            elif repo.split("/")[0] in (src.owner, *args.old_owner):
                kind = "same owner: candidate for a follow-up migration"
            else:
                kind = "other owner"
            print(f"  {repo} ({kind})")
            for w in where:
                print(f"    {w}")
    if not args.apply:
        if new_texts:
            print(f"\nDry run only. Re-run with --apply to rewrite {len(new_texts)} file(s).")
        return
    for path, text in new_texts.items():
        with open(os.path.join(args.dir, path), "w", encoding="utf-8", newline="") as f:
            f.write(text)
    print(f"rewrote {len(new_texts)} file(s); review them with git diff")


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
    commands = {
        "inspect": cmd_inspect,
        "create-repo": cmd_create_repo,
        "issues": cmd_issues,
        "milestones": cmd_milestones,
        "releases": cmd_releases,
        "links": cmd_links,
        "verify": cmd_verify,
    }
    for name in commands:
        s = sub.add_parser(name)
        s.add_argument("url", help="repository URL on the Forgejo instance (web, HTTPS or SSH)")
        s.add_argument(
            "--github",
            metavar="OWNER/NAME",
            help="target repository (default: the same-name GitHub org if you may create repositories there, "
            "else your gh account; for verify: the source's website field if it points at GitHub)",
        )
        if name in ("create-repo", "issues", "milestones", "releases", "links"):
            s.add_argument("--apply", action="store_true", help="write (default: dry run)")
        if name == "create-repo":
            s.add_argument("--description", help="use this instead of the source description")
        if name in ("issues", "milestones", "releases", "links"):
            s.add_argument("--old-owner", action="append", default=[], metavar="NAME",
                           help="former name of the owner account; links using it are rewritten (repeatable)")
        if name == "issues":
            s.add_argument("--dump-plan", metavar="FILE", help="write the full plan as JSON for review")
        if name == "links":
            s.add_argument("--dir", required=True, help="git worktree of the repository whose files to rewrite")
    args = p.parse_args(argv)
    src = Source(args.url, os.environ.get("FORGEJO_TOKEN"))
    from_website = website_target(src.api(f"repos/{src.full}").get("website")) if args.cmd == "verify" else None
    if args.github:
        gh_repo, args.target_reason = args.github, None
    elif from_website:
        gh_repo, args.target_reason = from_website, "from the website field"
    else:
        gh_repo, args.target_reason = default_target(src)
    if args.target_reason and args.cmd != "inspect":
        print(f"github target: {gh_repo} ({args.target_reason})", file=sys.stderr)
    commands[args.cmd](args, src, gh_repo)


if __name__ == "__main__":
    main()
