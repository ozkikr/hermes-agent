#!/usr/bin/env python3
"""Rebase a local patch branch onto the latest upstream release tag.

Typical usage:

    python scripts/update_local_patches_release.py
    python scripts/update_local_patches_release.py --tag v2026.4.8
    python scripts/update_local_patches_release.py \
        --test-command '. venv/bin/activate && python -m pytest -n0 tests/run_agent/test_fallback_model.py -q'
    python scripts/update_local_patches_release.py --dry-run
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import date
from pathlib import Path
from typing import Iterable

REPO_ROOT = Path(__file__).resolve().parent.parent


class CommandError(RuntimeError):
    """Raised when an external command fails."""


def run_command(
    args: list[str],
    *,
    cwd: Path = REPO_ROOT,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    """Run a command and optionally raise a formatted error."""
    result = subprocess.run(
        args,
        cwd=str(cwd),
        capture_output=True,
        text=True,
    )
    if check and result.returncode != 0:
        cmd = " ".join(args)
        details = "\n".join(
            part
            for part in (result.stdout.strip(), result.stderr.strip())
            if part
        )
        raise CommandError(f"{cmd} failed\n{details}".strip())
    return result


def run_shell(command: str, *, cwd: Path = REPO_ROOT) -> None:
    """Run a shell command using the user's shell."""
    shell = os.environ.get("SHELL") or "/bin/sh"
    result = subprocess.run(
        [shell, "-lc", command],
        cwd=str(cwd),
        text=True,
    )
    if result.returncode != 0:
        raise CommandError(
            f"Verification command failed with exit code {result.returncode}: {command}"
        )


def git(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return run_command(["git", *args], check=check)


def git_stdout(*args: str, check: bool = True) -> str:
    return git(*args, check=check).stdout.strip()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Rebase a local patch branch onto the latest upstream release tag."
    )
    parser.add_argument(
        "--branch",
        default="local-patches",
        help="Local patch branch to update (default: local-patches).",
    )
    parser.add_argument(
        "--upstream-remote",
        default="origin",
        help="Remote that tracks NousResearch/hermes-agent release tags (default: origin).",
    )
    parser.add_argument(
        "--push-remote",
        help="Remote to push the rebased branch to. Defaults to the branch upstream remote.",
    )
    parser.add_argument(
        "--tag",
        help="Release tag to rebase onto. Defaults to the latest GitHub release tag.",
    )
    parser.add_argument(
        "--test-command",
        help="Optional verification command to run after the rebase and before the force-push.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would run without changing git state.",
    )
    return parser.parse_args()


def ensure_git_repo() -> None:
    result = git("rev-parse", "--show-toplevel")
    top = Path(result.stdout.strip()).resolve()
    if top != REPO_ROOT:
        raise CommandError(f"Expected repo root {REPO_ROOT}, got {top}")


def ensure_clean_worktree() -> None:
    status = git_stdout("status", "--porcelain")
    if status:
        raise CommandError(
            "Working tree is not clean. Commit or stash changes before rebasing.\n"
            f"{status}"
        )


def ensure_ref_exists(ref: str) -> None:
    result = git("rev-parse", "--verify", "--quiet", ref, check=False)
    if result.returncode != 0:
        raise CommandError(f"Missing git ref: {ref}")


def remote_exists(name: str) -> bool:
    return git("remote", "get-url", name, check=False).returncode == 0


def current_branch() -> str:
    return git_stdout("rev-parse", "--abbrev-ref", "HEAD")


def upstream_remote_for_branch(branch: str) -> str | None:
    result = git("rev-parse", "--abbrev-ref", f"{branch}@{{upstream}}", check=False)
    if result.returncode != 0:
        return None
    upstream_ref = result.stdout.strip()
    if "/" not in upstream_ref:
        return None
    return upstream_ref.split("/", 1)[0]


def switch_branch(branch: str, *, dry_run: bool) -> None:
    if current_branch() == branch:
        return
    if dry_run:
        print(f"[dry-run] git switch {branch}")
        return
    git("switch", branch)


def sanitize_branch_component(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", value.strip()).strip("-") or "unknown"


def git_ref_exists(ref: str) -> bool:
    return git("show-ref", "--verify", "--quiet", ref, check=False).returncode == 0


def build_backup_branch_name(
    branch: str,
    tag: str,
    *,
    today: date | None = None,
    existing_refs: Iterable[str] = (),
) -> str:
    safe_branch = sanitize_branch_component(branch)
    safe_tag = sanitize_branch_component(tag)
    day = (today or date.today()).isoformat().replace("-", "")
    base = f"backup/{safe_branch}-pre-{safe_tag}-{day}"

    existing = set(existing_refs)
    if base not in existing:
        return base

    suffix = 2
    while f"{base}-{suffix}" in existing:
        suffix += 1
    return f"{base}-{suffix}"


def next_backup_branch_name(branch: str, tag: str) -> str:
    refs = git_stdout("for-each-ref", "--format=%(refname:short)", "refs/heads")
    existing = refs.splitlines() if refs else []
    return build_backup_branch_name(branch, tag, existing_refs=existing)


def parse_github_repo(remote_url: str) -> tuple[str, str]:
    patterns = (
        r"^git@github\.com:(?P<owner>[^/]+)/(?P<repo>[^/]+?)(?:\.git)?$",
        r"^https://github\.com/(?P<owner>[^/]+)/(?P<repo>[^/]+?)(?:\.git)?/?$",
        r"^ssh://git@github\.com/(?P<owner>[^/]+)/(?P<repo>[^/]+?)(?:\.git)?$",
    )
    for pattern in patterns:
        match = re.match(pattern, remote_url)
        if match:
            return match.group("owner"), match.group("repo")
    raise CommandError(
        f"Unsupported GitHub remote URL format: {remote_url}\n"
        "Pass --tag explicitly if you do not want release auto-detection."
    )


def latest_release_tag(upstream_remote: str) -> str:
    remote_url = git_stdout("remote", "get-url", upstream_remote)
    owner, repo = parse_github_repo(remote_url)
    url = f"https://api.github.com/repos/{owner}/{repo}/releases/latest"
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "hermes-local-patches-updater",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise CommandError(
            f"Could not resolve latest GitHub release tag from {url}: {exc}"
        ) from exc

    tag = str(payload.get("tag_name") or "").strip()
    if not tag:
        raise CommandError(f"GitHub latest release response from {url} did not include tag_name")
    return tag


def latest_git_tag() -> str:
    tags = git_stdout("tag", "--list", "v*", "--sort=-v:refname")
    if not tags:
        raise CommandError("No local tags matching 'v*' were found after fetch --tags")
    return tags.splitlines()[0]


def warn_if_rerere_disabled() -> None:
    enabled = git_stdout("config", "--bool", "rerere.enabled", check=False)
    if enabled.lower() != "true":
        print("Tip: enable rerere once with `git config rerere.enabled true`.")


def print_plan(
    *,
    branch: str,
    upstream_remote: str,
    push_remote: str,
    tag: str,
    old_base: str,
    backup_branch: str,
    test_command: str | None,
    dry_run: bool,
) -> None:
    print(f"Branch:        {branch}")
    print(f"Upstream:      {upstream_remote}")
    print(f"Push remote:   {push_remote}")
    print(f"Target tag:    {tag}")
    print(f"Old base:      {old_base}")
    print(f"Backup branch: {backup_branch}")
    print(f"Verify:        {test_command or '(skipped)'}")
    if dry_run:
        print("Mode:          dry-run")


def main() -> int:
    args = parse_args()

    ensure_git_repo()
    ensure_clean_worktree()

    if not remote_exists(args.upstream_remote):
        raise CommandError(f"Unknown upstream remote: {args.upstream_remote}")

    push_remote = args.push_remote or upstream_remote_for_branch(args.branch)
    if not push_remote:
        raise CommandError(
            f"Branch {args.branch!r} has no upstream remote.\n"
            "Pass --push-remote explicitly."
        )
    if not remote_exists(push_remote):
        raise CommandError(f"Unknown push remote: {push_remote}")

    ensure_ref_exists(f"refs/heads/{args.branch}")
    warn_if_rerere_disabled()

    print(f"Fetching {args.upstream_remote} tags...")
    if not args.dry_run:
        git("fetch", args.upstream_remote, "--tags")
        if push_remote != args.upstream_remote:
            print(f"Fetching {push_remote}...")
            git("fetch", push_remote)

    switch_branch(args.branch, dry_run=args.dry_run)

    target_tag = args.tag
    if not target_tag:
        try:
            target_tag = latest_release_tag(args.upstream_remote)
        except CommandError as exc:
            print(f"Latest release lookup failed, falling back to local tags: {exc}")
            target_tag = latest_git_tag()

    ensure_ref_exists(target_tag)
    old_base = git_stdout("merge-base", args.branch, target_tag)
    backup_branch = next_backup_branch_name(args.branch, target_tag)

    print_plan(
        branch=args.branch,
        upstream_remote=args.upstream_remote,
        push_remote=push_remote,
        tag=target_tag,
        old_base=old_base,
        backup_branch=backup_branch,
        test_command=args.test_command,
        dry_run=args.dry_run,
    )

    if args.dry_run:
        return 0

    print(f"Pushing current {args.branch} to {push_remote}/{args.branch}...")
    git("push", "-u", push_remote, args.branch)

    print(f"Creating backup branch {backup_branch}...")
    git("branch", backup_branch, args.branch)

    print(f"Rebasing {args.branch} onto {target_tag}...")
    git("rebase", "--onto", target_tag, old_base, args.branch)

    if args.test_command:
        print("Running verification command...")
        run_shell(args.test_command)

    print(f"Force-pushing {args.branch} to {push_remote}/{args.branch} with lease...")
    git("push", "--force-with-lease", push_remote, args.branch)

    print("Done.")
    print(f"Backup branch preserved at {backup_branch}.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except CommandError as exc:
        print(str(exc), file=sys.stderr)
        if git("rev-parse", "--verify", "--quiet", "REBASE_HEAD", check=False).returncode == 0:
            print(
                "A rebase is in progress. Resolve conflicts, then run `git rebase --continue` "
                "or `git rebase --abort`.",
                file=sys.stderr,
            )
        raise SystemExit(1)
