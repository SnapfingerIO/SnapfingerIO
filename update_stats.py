#!/usr/bin/env python3
"""Write current GitHub stats into the profile card SVGs.

Uptime is the age of this GitHub account, counted from the day the account
was created. Every other number is a public total. Dot leaders shrink or
grow so each column stays the same width when a number gains or loses digits.
"""

from __future__ import annotations

import json
import os
import re
import sys
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SVG_FILES = (ROOT / "dark_mode.svg", ROOT / "light_mode.svg")
API_URL = "https://api.github.com/graphql"

# Each paired stat is label + dots + space + number, and that sum stays 26
# characters so the "|" between the two columns does not move.
COLUMN_WIDTH = 26
UPTIME_LABEL = ". Uptime: "
UPTIME_LINE_WIDTH = 57
BAR_WIDTH = 54
BLOCK = "\u2588"

STAT_LABELS = {
    "repos": ". Repos: ",
    "stars": ". Stars: ",
    "forks": ". Forks: ",
    "followers": ". Followers: ",
    "commits": ". Commits: ",
    "contributed": ". Contributed: ",
    "prs": ". PRs: ",
    "issues": ". Issues: ",
    "contributions": ". Contributions: ",
    "reviews": ". Reviews: ",
}

LINE_WIDTHS = {
    "uptime-value": UPTIME_LINE_WIDTH,
    "repos-value": 55,
    "forks-value": 55,
    "commits-value": 55,
    "prs-value": 55,
    "contributions-value": 55,
    "contributions-bar": BAR_WIDTH,
}

PROFILE_QUERY = """
query($login: String!, $cursor: String) {
  user(login: $login) {
    createdAt
    followers { totalCount }
    pullRequests { totalCount }
    issues { totalCount }
    repositories(
      first: 100
      after: $cursor
      privacy: PUBLIC
      ownerAffiliations: [OWNER]
    ) {
      totalCount
      pageInfo { hasNextPage endCursor }
      nodes { stargazerCount forkCount }
    }
  }
}
"""

CONTRIBUTIONS_QUERY = """
query($login: String!, $from: DateTime!, $to: DateTime!) {
  user(login: $login) {
    contributionsCollection(from: $from, to: $to) {
      totalCommitContributions
      totalPullRequestReviewContributions
      contributionCalendar { totalContributions }
      commitContributionsByRepository(maxRepositories: 100) {
        repository { nameWithOwner owner { login } }
      }
      issueContributionsByRepository(maxRepositories: 100) {
        repository { nameWithOwner owner { login } }
      }
      pullRequestContributionsByRepository(maxRepositories: 100) {
        repository { nameWithOwner owner { login } }
      }
      pullRequestReviewContributionsByRepository(maxRepositories: 100) {
        repository { nameWithOwner owner { login } }
      }
    }
  }
}
"""

REPO_LIST_FIELDS = (
    "commitContributionsByRepository",
    "issueContributionsByRepository",
    "pullRequestContributionsByRepository",
    "pullRequestReviewContributionsByRepository",
)


def graphql(query: str, variables: dict) -> dict:
    token = os.environ.get("GITHUB_TOKEN", "")
    if not token:
        raise SystemExit("GITHUB_TOKEN is not set.")
    payload = json.dumps({"query": query, "variables": variables}).encode("utf-8")
    request = urllib.request.Request(
        API_URL,
        data=payload,
        method="POST",
        headers={
            "Authorization": "bearer " + token,
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "snapfingerio-update-stats",
        },
    )
    try:
        with urllib.request.urlopen(request) as response:
            body = json.load(response)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")
        raise SystemExit("GitHub API HTTP %s: %s" % (exc.code, detail)) from exc
    if body.get("errors"):
        raise SystemExit("GitHub API error: " + json.dumps(body["errors"]))
    return body["data"]


def parse_github_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def to_github_time(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def add_months(start: date, months: int) -> date:
    month_index = start.month - 1 + months
    year = start.year + month_index // 12
    month = month_index % 12 + 1
    if month == 12:
        following = date(year + 1, 1, 1)
    else:
        following = date(year, month + 1, 1)
    last_day = (following - timedelta(days=1)).day
    return date(year, month, min(start.day, last_day))


def format_uptime(created: date, today: date) -> str:
    """Calendar age from the account creation date, such as '2 months, 12 days'."""
    if today < created:
        today = created
    years = today.year - created.year
    if add_months(created, years * 12) > today:
        years -= 1
    months = 0
    while add_months(created, years * 12 + months + 1) <= today:
        months += 1
    anchor = add_months(created, years * 12 + months)
    days = (today - anchor).days

    parts = []
    if years:
        parts.append("%d year%s" % (years, "" if years == 1 else "s"))
    if months:
        parts.append("%d month%s" % (months, "" if months == 1 else "s"))
    if days or not parts:
        parts.append("%d day%s" % (days, "" if days == 1 else "s"))
    return ", ".join(parts)


def year_windows(start: datetime, end: datetime):
    """Split [start, end] into slices of at most one year, which is the API limit."""
    cursor = start
    while cursor < end:
        window_end = min(cursor + timedelta(days=365) - timedelta(seconds=1), end)
        if window_end <= cursor:
            break
        yield cursor, window_end
        cursor = window_end + timedelta(seconds=1)


def remember_other_repos(collection: dict, login: str, found: set) -> None:
    for field in REPO_LIST_FIELDS:
        items = collection.get(field) or []
        if len(items) >= 100:
            print(
                "Warning: %s returned 100 repositories for one year; "
                "Contributed may be short." % field,
                file=sys.stderr,
            )
        for item in items:
            repo = item.get("repository") or {}
            owner = (repo.get("owner") or {}).get("login") or ""
            name = repo.get("nameWithOwner") or ""
            if not name or owner.lower() == login.lower():
                continue
            found.add(name.lower())


def fetch_stats(login: str) -> dict:
    """Public stats for the account. Private activity is invisible to GITHUB_TOKEN."""
    cursor = None
    created_at = None
    followers = prs = issues = repos = stars = forks = 0
    while True:
        data = graphql(PROFILE_QUERY, {"login": login, "cursor": cursor})
        user = data.get("user")
        if user is None:
            raise SystemExit("GitHub user %s was not found." % login)
        page = user["repositories"]
        if created_at is None:
            created_at = user["createdAt"]
            followers = user["followers"]["totalCount"]
            prs = user["pullRequests"]["totalCount"]
            issues = user["issues"]["totalCount"]
            repos = page["totalCount"]
        for node in page["nodes"] or []:
            if not node:
                continue
            stars += node["stargazerCount"]
            forks += node["forkCount"]
        info = page["pageInfo"]
        if not info["hasNextPage"]:
            break
        cursor = info["endCursor"]

    created = parse_github_time(created_at)
    now = datetime.now(timezone.utc).replace(microsecond=0)
    commits = 0
    contributed: set = set()
    for start, end in year_windows(created, now):
        data = graphql(
            CONTRIBUTIONS_QUERY,
            {"login": login, "from": to_github_time(start), "to": to_github_time(end)},
        )
        collection = data["user"]["contributionsCollection"]
        commits += collection["totalCommitContributions"]
        remember_other_repos(collection, login, contributed)

    recent_start = now - timedelta(days=365) + timedelta(seconds=1)
    recent = graphql(
        CONTRIBUTIONS_QUERY,
        {
            "login": login,
            "from": to_github_time(recent_start),
            "to": to_github_time(now),
        },
    )["user"]["contributionsCollection"]

    return {
        "uptime": format_uptime(created.date(), now.date()),
        "repos": repos,
        "stars": stars,
        "forks": forks,
        "followers": followers,
        "commits": commits,
        "contributed": len(contributed),
        "prs": prs,
        "issues": issues,
        "contributions": recent["contributionCalendar"]["totalContributions"],
        "reviews": recent["totalPullRequestReviewContributions"],
    }


def xml_text(value: str) -> str:
    return value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def visible_text(value: str) -> str:
    return value.replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")


def column_pieces(label: str, number: int) -> tuple:
    shown = " " + str(int(number))
    dots = COLUMN_WIDTH - len(label) - len(shown)
    if dots < 0:
        dots = 0
    return "." * dots, shown


def uptime_pieces(uptime: str) -> tuple:
    shown = " " + uptime
    dots = UPTIME_LINE_WIDTH - len(UPTIME_LABEL) - len(shown)
    if dots < 0:
        dots = 0
    return "." * dots, shown


def contribution_bar(count: int) -> str:
    filled = max(0, min(int(count), BAR_WIDTH))
    return BLOCK * filled + " " * (BAR_WIDTH - filled)


def stat_updates(stats: dict) -> dict:
    updates = {}
    dots, value = uptime_pieces(stats["uptime"])
    updates["uptime-dots"] = dots
    updates["uptime-value"] = value
    for key, label in STAT_LABELS.items():
        dots, value = column_pieces(label, stats[key])
        updates[key + "-dots"] = dots
        updates[key + "-value"] = value
    updates["contributions-bar"] = contribution_bar(stats["contributions"])
    return updates


def replace_tspan(svg: str, element_id: str, text: str) -> str:
    pattern = re.compile(
        r'(<tspan\b(?=[^>]*\bid="%s")[^>]*>)(.*?)(</tspan>)' % re.escape(element_id),
        re.DOTALL,
    )
    updated, count = pattern.subn(lambda match: match.group(1) + xml_text(text) + match.group(3), svg, count=1)
    if count != 1:
        raise SystemExit("Expected one tspan with id %s." % element_id)
    return updated


def line_containing(svg: str, element_id: str) -> str:
    match = re.search(
        r"<text\b[^>]*>(?:(?!</text>).)*\bid=\"%s\".*?</text>" % re.escape(element_id),
        svg,
        re.DOTALL,
    )
    if not match:
        raise SystemExit("Could not find the line for id %s." % element_id)
    parts = re.findall(r"<tspan\b[^>]*>(.*?)</tspan>", match.group(0))
    return "".join(visible_text(part) for part in parts)


def apply_stats(svg: str, stats: dict) -> str:
    updated = svg
    for element_id, text in stat_updates(stats).items():
        updated = replace_tspan(updated, element_id, text)
    for element_id, width in LINE_WIDTHS.items():
        length = len(line_containing(updated, element_id))
        if length != width:
            raise SystemExit(
                "Line %s is %d characters after the update; it should stay %d."
                % (element_id, length, width)
            )
    return updated


def update_file(path: Path, stats: dict) -> None:
    original = path.read_text(encoding="utf-8")
    updated = apply_stats(original, stats)
    if updated != original:
        path.write_text(updated, encoding="utf-8")


def resolve_login() -> str:
    login = os.environ.get("GITHUB_REPOSITORY_OWNER") or os.environ.get("GITHUB_LOGIN")
    if login:
        return login
    sample = (ROOT / "dark_mode.svg").read_text(encoding="utf-8")
    match = re.search(r"github\.com/([A-Za-z0-9-]+)", sample)
    if match:
        return match.group(1)
    raise SystemExit("Set GITHUB_REPOSITORY_OWNER to the GitHub username.")


def main() -> None:
    login = resolve_login()
    stats = fetch_stats(login)
    for path in SVG_FILES:
        update_file(path, stats)
    print("Updated %s" % login)
    for key in ("uptime",) + tuple(STAT_LABELS):
        print("  %s: %s" % (key, stats[key]))


if __name__ == "__main__":
    main()
