#!/usr/bin/env python3
"""bootstrap-repo.py -- Create missing repo-local shared-memory wiring.

Creates the directory structure and symlink that the shared repo-memory system
requires inside any git repository that wants to use it.  Idempotent: running
it on an already-wired repo is safe and produces no output.

Called automatically by session-start.py whenever repo_wiring_issues() returns
a non-empty list.  Can also be run manually to repair a partially-wired repo.

What this script creates (all relative to the repo root):
  .agents/memory/adr/                     -- ADR storage directory
  .agents/memory/daily/                   -- daily event shard storage
  .agents/memory/pending/                 -- ignored raw shard staging area
  .agents/memory/state/                   -- ignored derived episode-graph state
  .codex/local/                           -- local catch-up state (never committed)
  .claude/local/                          -- Claude-specific local state
  .githooks/                              -- git hooks directory
  .githooks/pre-commit                    -- blocks raw/pending shards, then
                                            delegates to optional project checks
  .githooks/post-checkout                 -- rebuilds local catch-up after checkout
  .githooks/post-merge                    -- rebuilds local catch-up after merge/pull
  .githooks/post-rewrite                  -- rebuilds local catch-up after rebase/rewrite
  .codex/memory -> ../.agents/memory      -- Codex access-path symlink
  .agents/memory/adr/INDEX.md             -- empty ADR index table
  .gitignore entries for local-only shared-memory state
  git config core.hooksPath = .githooks   -- points git at the hooks directory

Usage:
  bootstrap-repo.py [--repo-root <path>] [--dry-run]
"""
from __future__ import annotations

import argparse
import os
import subprocess
from pathlib import Path

from common import (
    GITHOOKS_RELATIVE_DIR,
    PROJECT_PRE_COMMIT_RELATIVE_PATH,
    REQUIRED_GITIGNORE_ENTRIES,
    SHARED_REPO_MEMORY_SYSTEM_VERSION,
    ensure_dir,
    format_log_prefix,
    missing_gitignore_entries,
    safe_main,
    set_runtime_log_context,
    try_repo_root,
    warn,
    write_text,
)

# Expected relative target for the .codex/memory symlink.
# This must match the value validated by session-start.py's repo_wiring_issues().
EXPECTED_MEMORY_TARGET = "../.agents/memory"

# Initial content for the ADR index file.
# promote-adr.py rebuilds this table from scratch after every ADR promotion.
_INDEX_INITIAL = """\
# ADR index

| ADR | Title | Status | Date | Tags | Must Read | Supersedes | Superseded By |
|---|---|---|---|---|---|---|---|
| - | None | - | - | - | - | - | - |
"""

_GIT_HOOK_NAMES: tuple[str, ...] = (
    "pre-commit",
    "post-checkout",
    "post-merge",
    "post-rewrite",
)


_ALL_AGENTS: tuple[str, ...] = ("claude", "codex", "gemini")


def _parse_agents(value: str) -> set[str]:
    """Parse a comma-separated agents list, validating each entry."""
    agents = {a.strip() for a in value.split(",") if a.strip()}
    unknown = agents - set(_ALL_AGENTS)
    if unknown:
        raise argparse.ArgumentTypeError(
            f"unknown agents: {','.join(sorted(unknown))}. "
            f"Valid: {','.join(_ALL_AGENTS)}"
        )
    return agents


def parse_args() -> argparse.Namespace:
    """Parse command-line arguments.

    Returns:
        argparse.Namespace: Parsed arguments with optional repo_root, dry_run,
        and agents (scope of agent-specific scaffolding to create).
    """
    parser = argparse.ArgumentParser(
        description="Bootstrap repo-local shared-memory wiring."
    )
    parser.add_argument(
        "--repo-root",
        help="Path to the repository root.  Defaults to git rev-parse --show-toplevel.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print each action without modifying any files.",
    )
    parser.add_argument(
        "--agents",
        type=_parse_agents,
        default=set(_ALL_AGENTS),
        help=(
            "Comma-separated agents whose scaffolding should be created. "
            "Default: all three (claude,codex,gemini) for backwards-compatible "
            "global behavior. A per-repo Claude install passes --agents claude."
        ),
    )
    return parser.parse_args()


def log(message: str, *, dry_run: bool = False) -> None:
    """Print a repo-bootstrap log line with runtime metadata.

    Args:
        message: Human-readable action description.
        dry_run: When True, prefixes the message with "[DRY-RUN]".
    """
    prefix: str = "[DRY-RUN] " if dry_run else ""
    print(f"{format_log_prefix()} {prefix}{message}")


def ensure_symlink(link_path: Path, target: str, *, dry_run: bool) -> None:
    """Create or repair the symlink at link_path pointing to target.

    Removes any existing file, directory, or stale symlink at link_path before
    creating the new symlink.  Content is canonical in .agents/memory/ so the
    removal is safe.

    Args:
        link_path: Absolute path where the symlink should live.
        target: Relative symlink target string, e.g. "../.agents/memory".
        dry_run: When True, log the action without touching the filesystem.
    """
    current_target = None
    if link_path.is_symlink():
        try:
            current_target = link_path.readlink().as_posix()
        except OSError:
            pass

    if current_target == target:
        # Already correctly wired; nothing to do.
        return

    log(f"creating symlink {link_path} -> {target}", dry_run=dry_run)
    if dry_run:
        return

    # Remove whatever is at link_path (file, dir, or stale symlink) before creating.
    if link_path.exists() or link_path.is_symlink():
        if link_path.is_dir() and not link_path.is_symlink():
            import shutil

            shutil.rmtree(link_path)
        else:
            link_path.unlink()
    os.symlink(target, link_path)


def set_git_hooks_path(repo_root: Path, hooks_dir: str, *, dry_run: bool) -> None:
    """Set git's core.hooksPath to hooks_dir so the repo-local hooks fire.

    Args:
        repo_root: Absolute path to the repository root.
        hooks_dir: Relative path to the hooks directory, e.g. ".githooks".
        dry_run: When True, log the action without running git config.
    """
    # Check whether the value is already correct before writing.
    result = subprocess.run(
        ["git", "config", "--get", "core.hooksPath"],
        cwd=str(repo_root),
        check=False,
        capture_output=True,
        text=True,
    )
    if result.stdout.strip() == hooks_dir:
        return

    log(f"setting git config core.hooksPath = {hooks_dir}", dry_run=dry_run)
    if not dry_run:
        subprocess.run(
            ["git", "config", "core.hooksPath", hooks_dir],
            cwd=str(repo_root),
            check=True,
        )


def _gitignore_entries_for(agents: set[str]) -> tuple[str, ...]:
    """Filter REQUIRED_GITIGNORE_ENTRIES to the entries relevant for ``agents``.

    Entries that live under an agent-specific top-level directory (``.codex/``,
    ``.gemini/``, ``.claude/``) are dropped when that agent is not in scope.
    Comment headers and agent-agnostic entries (``.githooks/``,
    ``.agents/memory/...``) are always kept so a per-repo Claude install still
    documents and ignores the data-bootstrap paths.
    """
    filtered: list[str] = []
    for entry in REQUIRED_GITIGNORE_ENTRIES:
        if entry.startswith(".codex/") and "codex" not in agents:
            continue
        if entry.startswith(".gemini/") and "gemini" not in agents:
            continue
        if entry.startswith(".claude/") and "claude" not in agents:
            continue
        filtered.append(entry)
    return tuple(filtered)


def ensure_gitignore(
    repo_root: Path, agents: set[str], *, dry_run: bool
) -> None:
    """Append missing agent-local ignore entries to the repo's .gitignore.

    Reads the existing .gitignore (if any), checks which entries (filtered to
    the agents in scope) are missing, and appends only the missing ones.
    Creates the file if absent.

    Args:
        repo_root: Absolute path to the repository root.
        agents: Agents whose scaffolding is being bootstrapped. Used to filter
            out gitignore entries for other agents.
        dry_run: When True, log the action without modifying the filesystem.
    """
    gitignore_path = repo_root / ".gitignore"
    str_existing_text: str = ""
    if gitignore_path.exists():
        str_existing_text = gitignore_path.read_text(encoding="utf-8")

    list_str_missing_entries: list[str] = missing_gitignore_entries(
        repo_root, _gitignore_entries_for(agents)
    )
    if not list_str_missing_entries:
        return

    log(
        f"appending {len(list_str_missing_entries)} entries to .gitignore",
        dry_run=dry_run,
    )
    if dry_run:
        return

    # Ensure a blank line before our block if the file doesn't end with one.
    separator = (
        "\n" if str_existing_text and not str_existing_text.endswith("\n\n") else ""
    )
    with gitignore_path.open("a", encoding="utf-8") as f:
        f.write(separator + "\n".join(list_str_missing_entries) + "\n")


def git_hook_text(str_hook_name: str) -> str:
    """Return the canonical repo-local Git hook script for one shared-memory hook.

    Args:
        str_hook_name: Git hook filename and trigger label. Supported values are
            "pre-commit", "post-checkout", "post-merge", and "post-rewrite".

    Returns:
        str: Full shell script text for the requested Git hook.
    """
    str_provenance_comment: str = "\n".join(
        (
            f"# Generated by agentmemory v{SHARED_REPO_MEMORY_SYSTEM_VERSION}.",
            "# This repo-local hook is created by the agentmemory SessionStart",
            "# bootstrap flow, typically during the first agent session opened in",
            "# this repository, and is repaired automatically if it drifts.",
            "# Do not edit this file manually.",
        )
    )
    if str_hook_name == "pre-commit":
        str_script = f"""#!/usr/bin/env bash
{str_provenance_comment}
set -euo pipefail

repo_root="$(git rev-parse --show-toplevel)"
export AGENTMEMORY_RUNTIME_ID="pre-commit"
export AGENTMEMORY_RUNTIME_VERSION="n/a"
python3 "$HOME/.agent/shared-repo-memory/pre-commit-memory-guard.py" --repo-root "$repo_root"
project_hook="$repo_root/{PROJECT_PRE_COMMIT_RELATIVE_PATH}"
if [ -f "$project_hook" ]; then
    bash "$project_hook" "$@"
fi
"""
        return str_script  # Normal exit.

    str_script = f"""#!/usr/bin/env bash
{str_provenance_comment}
set -euo pipefail

repo_root="$(git rev-parse --show-toplevel)"
export AGENTMEMORY_RUNTIME_ID="git-hook"
export AGENTMEMORY_RUNTIME_VERSION="n/a"
log_prefix="$("$HOME/.agent/shared-repo-memory/runtime-log-prefix.sh" 2>/dev/null || printf '[agentmemory][version={SHARED_REPO_MEMORY_SYSTEM_VERSION}][runtime=git-hook][runtime-version=n/a]')"
if ! python3 "$HOME/.agent/shared-repo-memory/build-catchup.py" --repo-root "$repo_root" --trigger {str_hook_name}; then
    echo "$log_prefix warning: {str_hook_name} memory catch-up failed (non-fatal)" >&2
fi
"""
    return str_script  # Normal exit.


def ensure_git_hooks(repo_root: Path, *, dry_run: bool) -> None:
    """Create or repair the repo-local Git hook scripts used by shared memory.

    Args:
        repo_root: Absolute path to the repository root.
        dry_run: When True, log the action without modifying files.

    Returns:
        None: Hook files are updated in place when needed.
    """
    hooks_dir: Path = repo_root / GITHOOKS_RELATIVE_DIR

    # Ensure each required Git hook exists with the canonical shared-memory command.
    for str_hook_name in _GIT_HOOK_NAMES:
        hook_path: Path = hooks_dir / str_hook_name
        str_expected_text: str = git_hook_text(str_hook_name)
        bool_needs_write: bool = True
        if hook_path.exists():
            str_current_text: str = hook_path.read_text(encoding="utf-8")
            bool_needs_write = str_current_text != str_expected_text
        if bool_needs_write:
            log(f"writing Git hook {hook_path.relative_to(repo_root)}", dry_run=dry_run)
            if not dry_run:
                write_text(hook_path, str_expected_text)
        if not dry_run and hook_path.exists():
            hook_path.chmod(hook_path.stat().st_mode | 0o111)


def main() -> int:
    """Bootstrap repo-local wiring.

    Returns:
        int: 0 on success; 1 if the repo root cannot be determined.
    """
    set_runtime_log_context("bootstrap", "n/a")
    args = parse_args()
    dry_run = args.dry_run

    repo_root = try_repo_root(args.repo_root)
    if repo_root is None:
        warn("bootstrap-repo.py: current directory is not inside a git repository")
        return 1

    agents: set[str] = args.agents

    # Agent-agnostic directories always created.
    list_str_rel_paths: list[str] = [
        ".agents/memory/adr",
        ".agents/memory/daily",
        ".agents/memory/pending",
        ".agents/memory/state",
        GITHOOKS_RELATIVE_DIR,
    ]
    # Agent-specific scratch dirs added only when the agent is in scope.
    if "claude" in agents:
        list_str_rel_paths.append(".claude/local")
    if "codex" in agents:
        list_str_rel_paths.append(".codex/local")

    for rel_path in list_str_rel_paths:
        target = repo_root / rel_path
        if not target.exists():
            log(f"creating directory {rel_path}", dry_run=dry_run)
            if not dry_run:
                ensure_dir(target)

    # The .codex/memory access-path symlink exists only when Codex is in scope.
    if "codex" in agents:
        ensure_symlink(
            repo_root / ".codex" / "memory",
            EXPECTED_MEMORY_TARGET,
            dry_run=dry_run,
        )

    # Write the initial ADR index only when the file does not already exist.
    index_path = repo_root / ".agents" / "memory" / "adr" / "INDEX.md"
    if not index_path.exists():
        log("creating .agents/memory/adr/INDEX.md", dry_run=dry_run)
        if not dry_run:
            write_text(index_path, _INDEX_INITIAL)

    # Ensure .gitignore covers local-only paths that should never be committed.
    ensure_gitignore(repo_root, agents, dry_run=dry_run)

    # Install the repo-local shared-memory hooks, including the commit guard.
    ensure_git_hooks(repo_root, dry_run=dry_run)

    # Point git at .githooks so the shared-memory hook set fires.
    set_git_hooks_path(repo_root, GITHOOKS_RELATIVE_DIR, dry_run=dry_run)

    log("repository setup complete")
    print(f"  repo: {repo_root}")
    hooks_path = subprocess.run(
        ["git", "config", "--get", "core.hooksPath"],
        cwd=str(repo_root),
        check=False,
        capture_output=True,
        text=True,
    ).stdout.strip()
    print(f"  Git hooks folder: {hooks_path or GITHOOKS_RELATIVE_DIR}")
    return 0


if __name__ == "__main__":
    raise SystemExit(safe_main(main, "BootstrapRepo"))
