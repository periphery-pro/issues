#!/usr/bin/env python3
"""Migrate open issues from the open-source Periphery repo into this tracker.

Each source issue becomes one new issue here. The original description and every
comment are collapsed into the new issue body, preserving authorship and dates,
with a link back to the original issue.

Requires the `gh` CLI, authenticated with read access to the source repo and
write access to the target repo.

Examples:

    # Preview everything without touching the target repo
    scripts/migrate_issues.py --dry-run

    # Migrate two specific issues
    scripts/migrate_issues.py --issue 1141 --issue 1142

    # Migrate everything, recording old URL -> new URL
    scripts/migrate_issues.py --map-file migration-map.json

Labels are translated via LABEL_MAP below; a source label with no mapping stops
the run so it can be given one.

Re-running is safe: issues that have already been migrated are detected via a
marker in the body and skipped.
"""

import argparse
import json
import re
import subprocess
import sys
import time
from pathlib import Path

DEFAULT_SOURCE = "peripheryapp/periphery"
DEFAULT_TARGET = "periphery-pro/issues"

# Source label -> label in this repo. Any source label missing from this table
# aborts the migration, so new upstream labels get a deliberate decision rather
# than being silently dropped.
LABEL_MAP = {
    "needs investigation": "triage",
    "enhancement": "enhancement",
    "awaiting feedback": "question",
}

# GitHub rejects issue bodies longer than this.
MAX_BODY_CHARS = 65536

MARKER_RE = re.compile(r"<!-- migrated-from: (?P<repo>[^#\s]+)#(?P<number>\d+) -->")

# Fenced code blocks and inline code spans, so rewrites can skip over them.
CODE_SEGMENT_RE = re.compile(r"(```.*?```|~~~.*?~~~|`[^`\n]*`)", re.DOTALL)
MENTION_RE = re.compile(r"(?<![A-Za-z0-9_/@-])@([A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?)")
ISSUE_REF_RE = re.compile(r"(?<![A-Za-z0-9_/&#-])#(\d+)\b")


class GhError(RuntimeError):
    pass


def gh(args, stdin=None):
    """Run a `gh` command and return its stdout."""
    result = subprocess.run(
        ["gh", *args],
        input=stdin,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise GhError(f"gh {' '.join(args)} failed:\n{result.stderr.strip()}")
    return result.stdout


def gh_json(args, stdin=None):
    output = gh(args, stdin=stdin).strip()
    return json.loads(output) if output else None


def paginate(path, per_page=100):
    """Fetch every page of a list endpoint, one request at a time."""
    items = []
    page = 1
    while True:
        separator = "&" if "?" in path else "?"
        batch = gh_json(["api", f"{path}{separator}per_page={per_page}&page={page}"])
        if not batch:
            break
        items.extend(batch)
        if len(batch) < per_page:
            break
        page += 1
    return items


def rewrite_outside_code(text, rewrite):
    """Apply `rewrite` to the parts of `text` that aren't code."""
    parts = CODE_SEGMENT_RE.split(text)
    # re.split with a capturing group puts the code segments at odd indices.
    for index in range(0, len(parts), 2):
        parts[index] = rewrite(parts[index])
    return "".join(parts)


def sanitize(text, source_repo):
    """Neutralize @mentions and repoint bare issue references at the source repo.

    Without this, migrating notifies everyone quoted in the original thread, and
    references like `#123` would silently resolve to unrelated issues here.
    """
    if not text:
        return text

    def rewrite(chunk):
        chunk = MENTION_RE.sub(r"`@\1`", chunk)
        return ISSUE_REF_RE.sub(rf"{source_repo}#\1", chunk)

    return rewrite_outside_code(text, rewrite)


def format_date(timestamp):
    return timestamp.split("T")[0] if timestamp else "an unknown date"


def user_link(user):
    login = (user or {}).get("login") or "ghost"
    # Linked rather than bare so GitHub doesn't treat it as a mention.
    return f"[@{login}](https://github.com/{login})"


def render_body(issue, comments, source_repo, raw):
    def prepare(text):
        text = (text or "").replace("\r\n", "\n").strip()
        return text if raw else sanitize(text, source_repo)

    number = issue["number"]
    marker = f"<!-- migrated-from: {source_repo}#{number} -->"
    header = (
        "> [!NOTE]\n"
        f"> Migrated from [{source_repo}#{number}]({issue['html_url']}), "
        f"opened by {user_link(issue.get('user'))} on {format_date(issue.get('created_at'))}."
    )

    description = prepare(issue.get("body")) or "_No description provided._"
    sections = [header, "", "## Original report", "", description]

    if comments:
        sections += ["", "---", "", f"## Comments ({len(comments)})"]
        for comment in comments:
            sections += [
                "",
                f"### {user_link(comment.get('user'))} commented on "
                f"{format_date(comment.get('created_at'))} "
                f"([link]({comment['html_url']}))",
                "",
                prepare(comment.get("body")) or "_No content._",
            ]

    body = "\n".join(sections)
    return truncate(body, issue["html_url"]) + f"\n\n{marker}\n"


def truncate(body, source_url):
    """Trim an over-long body, pointing at the original for the remainder."""
    notice = f"\n\n---\n\n_This thread was too long to copy in full. See the [original issue]({source_url}) for the remaining comments._"
    budget = MAX_BODY_CHARS - len(notice) - 200  # headroom for the marker
    if len(body) <= budget:
        return body
    return body[:budget].rsplit("\n", 1)[0] + notice


def fetch_open_issues(source_repo, numbers=None):
    """Fetch open issues, excluding pull requests, oldest first."""
    if numbers:
        issues = [gh_json(["api", f"repos/{source_repo}/issues/{number}"]) for number in numbers]
    else:
        issues = paginate(f"repos/{source_repo}/issues?state=open")
    issues = [issue for issue in issues if not issue.get("pull_request")]
    return sorted(issues, key=lambda issue: issue["number"])


def fetch_comments(source_repo, number):
    return paginate(f"repos/{source_repo}/issues/{number}/comments")


def already_migrated(target_repo, source_repo):
    """Return the source issue numbers that are already present in the target."""
    migrated = {}
    for issue in paginate(f"repos/{target_repo}/issues?state=all"):
        if issue.get("pull_request"):
            continue
        match = MARKER_RE.search(issue.get("body") or "")
        if match and match.group("repo") == source_repo:
            migrated[int(match.group("number"))] = issue["html_url"]
    return migrated


def target_labels(target_repo):
    return {label["name"] for label in paginate(f"repos/{target_repo}/labels")}


def source_labels(issue):
    return [label["name"] for label in issue.get("labels", [])]


def check_labels(issues, target_repo, ignore_unmapped):
    """Verify every source label maps to a label that exists in the target."""
    used = {name for issue in issues for name in source_labels(issue)}

    unmapped = sorted(used - LABEL_MAP.keys())
    if unmapped and not ignore_unmapped:
        print(
            "Error: no mapping for source label(s): "
            + ", ".join(f"'{name}'" for name in unmapped)
            + f"\nAdd them to LABEL_MAP in {Path(__file__).name}, "
            "or pass --ignore-unmapped-labels to drop them.",
            file=sys.stderr,
        )
        return False

    mapped = {LABEL_MAP[name] for name in used & LABEL_MAP.keys()}
    missing = sorted(mapped - target_labels(target_repo))
    if missing:
        print(
            "Error: LABEL_MAP points at label(s) that don't exist in "
            f"{target_repo}: " + ", ".join(f"'{name}'" for name in missing),
            file=sys.stderr,
        )
        return False

    if unmapped:
        print("Dropping unmapped label(s): " + ", ".join(f"'{name}'" for name in unmapped))
    return True


def create_issue(target_repo, title, body, labels):
    payload = {"title": title, "body": body, "labels": sorted(labels)}
    created = gh_json(
        ["api", f"repos/{target_repo}/issues", "--method", "POST", "--input", "-"],
        stdin=json.dumps(payload),
    )
    return created["html_url"]


def parse_args(argv):
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--source", default=DEFAULT_SOURCE, help=f"source repo (default: {DEFAULT_SOURCE})")
    parser.add_argument("--target", default=DEFAULT_TARGET, help=f"target repo (default: {DEFAULT_TARGET})")
    parser.add_argument(
        "--issue", type=int, action="append", dest="issues", metavar="N",
        help="migrate only this issue number, open or not (repeatable)",
    )
    parser.add_argument("--limit", type=int, help="migrate at most this many issues")
    parser.add_argument(
        "--dry-run", action="store_true",
        help="render the new issues without creating anything",
    )
    parser.add_argument(
        "--out-dir", type=Path, default=Path(".migration-preview"),
        help="where --dry-run writes rendered bodies (default: .migration-preview)",
    )
    parser.add_argument(
        "--ignore-unmapped-labels", action="store_true",
        help="drop source labels missing from LABEL_MAP instead of aborting",
    )
    parser.add_argument(
        "--raw", action="store_true",
        help="copy bodies verbatim, keeping live @mentions and bare #123 references",
    )
    parser.add_argument(
        "--delay", type=float, default=2.0,
        help="seconds to wait between creations, to stay under rate limits (default: 2)",
    )
    parser.add_argument("--map-file", type=Path, help="write an old URL -> new URL mapping here")
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)

    if args.raw:
        print("Warning: --raw keeps @mentions live; migrating will notify everyone quoted.\n")

    print(f"Fetching open issues from {args.source}...")
    issues = fetch_open_issues(args.source, args.issues)

    migrated = already_migrated(args.target, args.source)
    if migrated:
        print(f"{len(migrated)} issue(s) already migrated to {args.target}; they will be skipped.")

    pending = [issue for issue in issues if issue["number"] not in migrated]
    if args.limit:
        pending = pending[: args.limit]

    print(f"{len(pending)} issue(s) to migrate.\n")
    if not pending:
        return 0

    if not check_labels(pending, args.target, args.ignore_unmapped_labels):
        return 1

    if args.dry_run:
        args.out_dir.mkdir(parents=True, exist_ok=True)

    mapping = {}
    for index, issue in enumerate(pending, start=1):
        number = issue["number"]
        comments = fetch_comments(args.source, number) if issue.get("comments") else []
        body = render_body(issue, comments, args.source, args.raw)

        labels = {LABEL_MAP[name] for name in source_labels(issue) if name in LABEL_MAP}

        prefix = f"[{index}/{len(pending)}] #{number} {issue['title'][:70]}"
        summary = f"{len(comments)} comment(s), {len(body)} chars"
        if labels:
            summary += f", labels: {', '.join(sorted(labels))}"

        if args.dry_run:
            preview = args.out_dir / f"{number}.md"
            preview.write_text(f"# {issue['title']}\n\n{body}")
            print(f"{prefix}\n  {summary} -> {preview}")
            continue

        url = create_issue(args.target, issue["title"], body, labels)
        mapping[issue["html_url"]] = url
        print(f"{prefix}\n  {summary}\n  created {url}")
        if index < len(pending) and args.delay:
            time.sleep(args.delay)

    if args.map_file and mapping:
        args.map_file.write_text(json.dumps(mapping, indent=2) + "\n")
        print(f"\nWrote mapping for {len(mapping)} issue(s) to {args.map_file}")

    if args.dry_run:
        print(f"\nDry run: nothing was created. Rendered bodies are in {args.out_dir}/")

    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except GhError as error:
        print(f"\n{error}", file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        print("\nInterrupted. Re-run to resume; already-migrated issues are skipped.", file=sys.stderr)
        sys.exit(130)
