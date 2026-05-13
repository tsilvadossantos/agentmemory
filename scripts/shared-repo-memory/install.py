#!/usr/bin/env python3
"""install.py -- Install shared-repo-memory user-level assets and wire agent hooks.

Run via the repo root entry point:
    ./install.sh [--dry-run]

Or directly:
    python3 scripts/shared-repo-memory/install.py [--dry-run]

What this installer does:
  1. Creates ~/.agent/shared-repo-memory/ and copies all helper scripts into it.
  2. Creates ~/.agent/state/ and initializes shared_asset_refresh_state.json.
  3. Copies skills into ~/.agent/skills/ and creates per-agent symlinks under
     ~/.claude/skills/, ~/.codex/skills/, and ~/.gemini/skills/.
  4. Wires SessionStart and Stop (or AfterAgent) hooks for Claude Code, Codex,
     and Gemini CLI.

The --dry-run flag prints every action without making any changes, useful for
verifying what would be modified before committing to the install.

After installation, restart any open agent sessions.  The SessionStart hook
will validate and bootstrap repo-local wiring on the next session open.
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

from adapters import (
    ClaudeAdapter,
    CodexAdapter,
    CursorAdapter,
    GeminiAdapter,
    InstallerContext,
)
from agent_support import support_summary_lines
from common import format_log_prefix, set_runtime_log_context

# Scripts copied verbatim from scripts/shared-repo-memory/ into ~/.agent/shared-repo-memory/.
# The order here is for readability; installation processes them in sequence.
SCRIPTS = [
    "common.py",
    "runtime-log-prefix.sh",
    "models.py",
    "agent_support.py",
    "dedup.py",
    "episode_graph.py",
    "bootstrap-repo.py",
    "pre-commit-memory-guard.py",
    "session-start.py",
    "post-turn-notify.py",
    "prompt-guard.py",
    "post-compact.py",
    "auto-bootstrap.py",
    "rebuild-summary.py",
    "build-catchup.py",
    "promote-adr.py",
    "enrich-shard.py",
    "publish-checkpoint.py",
    "uninstall.py",
]

# Adapter package files, installed under adapters/ subdirectory.
ADAPTER_FILES = [
    "adapters/__init__.py",
    "adapters/claude.py",
    "adapters/codex.py",
    "adapters/cursor.py",
    "adapters/gemini.py",
]

# Ordered list of adapter classes for installation wiring.
_ADAPTERS = [ClaudeAdapter, CodexAdapter, CursorAdapter, GeminiAdapter]


def read_version(repo_root: Path) -> str:
    """Read the version string from pyproject.toml.

    Args:
        repo_root: Absolute path to the agentmemory repo root.

    Returns:
        str: Version string, e.g. "0.1.0", or "unknown" if not found.
    """
    pyproject = repo_root / "pyproject.toml"
    if pyproject.exists():
        match = re.search(
            r'^version\s*=\s*"([^"]+)"',
            pyproject.read_text(encoding="utf-8"),
            re.MULTILINE,
        )
        if match:
            return match.group(1)
    return "unknown"


def print_banner(version: str) -> None:
    """Print the install banner with version info.

    Args:
        version: Version string to display.
    """
    banner = f"""\

   ___                   _  ___  ___
  / _ \\                 | | |  \\/  |
 / /_\\ \\ __ _  ___ _ __ | |_| .  . | ___ _ __ ___   ___  _ __ _   _
 |  _  |/ _  |/ _ \\ '_ \\| __| |\\/| |/ _ \\ '_ ' _ \\ / _ \\| '__| | | |
 | | | | (_| |  __/ | | | |_| |  | |  __/ | | | | | (_) | |  | |_| |
 \\_| |_/\\__, |\\___|_| |_|\\__\\_|  |_/\\___|_| |_| |_|\\___/|_|   \\__, |
         __/ |                                                 __/ |
        |___/                                                 |___/

  v{version}  Shared Repo Memory System
  github.com/davecthomas/agentmemory
"""
    print(banner)


def log(message: str) -> None:
    """Print an installer log line with runtime metadata.

    Args:
        message: Human-readable message text.
    """
    print(f"{format_log_prefix()} {message}")


class Installer:
    """Orchestrates installation of all shared-repo-memory user assets and agent wiring.

    Each agent (Claude Code, Codex, Gemini) requires a different configuration
    format and hook event name.  This class handles them independently in
    _wire_claude, _wire_codex, and _wire_gemini so each can be updated without
    risking the others.

    The dry_run flag prevents any filesystem writes; callers can pass it to
    preview the full installation plan before executing it.
    """

    def __init__(
        self, repo_root: Path, dry_run: bool = False, force: bool = False
    ) -> None:
        """Initialize the installer with the repo root and flags.

        Args:
            repo_root: Absolute path to the agentmemory repository root.
                Script sources are read from repo_root/scripts/shared-repo-memory/.
            dry_run: When True, log all actions without making any changes.
            force: When True, overwrite existing installed skill copies.
        """
        self.repo_root = repo_root
        self.dry_run = dry_run
        self.force = force
        self.home = Path.home()
        # Install target: scripts are copied here, separate from any single agent's config.
        self.install_root = self.home / ".agent" / "shared-repo-memory"
        self.skills_root = self.home / ".agent" / "skills"
        self.claude_settings = self.home / ".claude" / "settings.json"
        self.codex_config = self.home / ".codex" / "config.toml"
        self.codex_hooks = self.home / ".codex" / "hooks.json"
        self.gemini_settings = self.home / ".gemini" / "settings.json"

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _same_content(self, a: Path, b: Path) -> bool:
        """Return True when both paths exist and have identical byte content.

        Args:
            a: First file path.
            b: Second file path.

        Returns:
            bool: True if both files exist and are byte-identical.
        """
        if not a.exists() or not b.exists():
            return False
        return a.read_bytes() == b.read_bytes()

    def _ensure_dir(self, path: Path) -> None:
        """Create path and any missing parents, or log the would-be action in dry-run mode.

        Args:
            path: Directory to create.
        """
        if self.dry_run:
            log(f"[DRY-RUN] would create {path}")
            return
        path.mkdir(parents=True, exist_ok=True)

    def _load_json(self, path: Path) -> dict:
        """Load a JSON file as a dict, returning {} on any error or absence.

        Args:
            path: JSON file to read.

        Returns:
            dict: Parsed JSON, or empty dict if the file is absent or invalid.
        """
        if not path.exists():
            return {}
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}

    def _save_json(self, path: Path, data: dict) -> None:
        """Write data to path as pretty-printed JSON, creating parents as needed.

        In dry-run mode, logs the path that would be written without touching disk.

        Args:
            path: Destination file path.
            data: JSON-serialisable dict to write.
        """
        if self.dry_run:
            log(f"[DRY-RUN] would write {path}")
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )

    # ------------------------------------------------------------------
    # Script installation
    # ------------------------------------------------------------------

    def _copy_scripts(self) -> None:
        """Copy all listed helper scripts from the repo source to the install target.

        Sets the executable bit on each destination file so scripts can be run
        directly without specifying the Python interpreter.  In dry-run mode,
        logs each copy operation without modifying files.
        """
        src_dir = self.repo_root / "scripts" / "shared-repo-memory"
        all_files = list(SCRIPTS) + list(ADAPTER_FILES)
        for name in all_files:
            src = src_dir / name
            dst = self.install_root / name
            if self.dry_run:
                action: str = (
                    "unchanged" if self._same_content(src, dst) else "would update"
                )
                log(f"[DRY-RUN] {action}: {name}")
            else:
                dst.parent.mkdir(parents=True, exist_ok=True)
                changed: bool = not self._same_content(src, dst)
                shutil.copy2(src, dst)
                # Ensure the script is executable for all hook runners.
                dst.chmod(dst.stat().st_mode | 0o111)
                status: str = "updated" if changed else "unchanged"
                log(f"script {name}: {status}")

    def _log_agent_support_summary(self) -> None:
        """Print the current supported hook surface for each agent runtime.

        Returns:
            None: Emits human-readable log lines so operators can see which
                agents have full support and which have explicit limitations.
        """
        list_str_summary_lines: list[str] = support_summary_lines()
        log("Agent support status:")
        for str_summary_line in list_str_summary_lines:
            log(f"  {str_summary_line}")

    # ------------------------------------------------------------------
    # Agent hook wiring (delegated to runtime adapters)
    # ------------------------------------------------------------------

    def _wire_agents(self) -> None:
        """Wire all agent runtime hooks by delegating to adapter modules.

        Each adapter knows its own config file format, hook event names, timeout
        conventions, and idempotency rules.  The installer just passes context.
        """
        ctx = InstallerContext(
            install_root=self.install_root,
            home=self.home,
            repo_root=self.repo_root,
            dry_run=self.dry_run,
            load_json=self._load_json,
            save_json=self._save_json,
        )
        for adapter in _ADAPTERS:
            adapter.wire_hooks(ctx)
            if not self.dry_run:
                log(f"wired {adapter.agent_id()} hooks")

    # ------------------------------------------------------------------
    # Skills installation
    # ------------------------------------------------------------------

    def _install_skills(self) -> None:
        """Copy skills from the repo into ~/.agent/skills/ and symlink into each agent.

        Each skill directory is copied verbatim from repo_root/skills/<skill>/ to
        ~/.agent/skills/<skill>/.  Per-agent symlinks are then created under
        ~/.claude/skills/, ~/.codex/skills/, and ~/.gemini/skills/.

        When force=True, existing copies in ~/.agent/skills/ are replaced.
        Symlinks are always (re)created since they are cheap and may be stale.
        """
        skills_src: Path = self.repo_root / "skills"
        if not skills_src.is_dir():
            log("no skills/ directory found in repo root — skipping skill install")
            return

        agent_skill_dirs: list[Path] = [
            self.home / ".claude" / "skills",
            self.home / ".codex" / "skills",
            self.home / ".gemini" / "skills",
        ]

        for skill_dir in skills_src.iterdir():
            if not skill_dir.is_dir():
                continue
            skill_name: str = skill_dir.name
            dest: Path = self.skills_root / skill_name

            if self.dry_run:
                if not dest.exists():
                    log(f"[DRY-RUN] skill {skill_name}: would install")
                else:
                    # Check if any file in the skill differs.
                    skill_files: list[Path] = list(skill_dir.iterdir())
                    needs_update: bool = any(
                        not self._same_content(f, dest / f.name)
                        for f in skill_files
                        if f.is_file()
                    )
                    dry_status: str = "would update" if needs_update else "unchanged"
                    log(f"[DRY-RUN] skill {skill_name}: {dry_status}")
            else:
                if not dest.exists():
                    shutil.copytree(skill_dir, dest)
                    log(f"skill {skill_name}: installed")
                else:
                    # Copy each file individually, tracking whether anything changed.
                    updated_files: list[str] = []
                    for src_file in skill_dir.iterdir():
                        if not src_file.is_file():
                            continue
                        dst_file: Path = dest / src_file.name
                        if not self._same_content(src_file, dst_file):
                            shutil.copy2(src_file, dst_file)
                            updated_files.append(src_file.name)
                    if updated_files:
                        log(f"skill {skill_name}: updated ({', '.join(updated_files)})")
                    else:
                        log(f"skill {skill_name}: unchanged")

            # Create per-agent symlinks into the canonical copy.
            for agent_skills_dir in agent_skill_dirs:
                link: Path = agent_skills_dir / skill_name
                if self.dry_run:
                    log(f"[DRY-RUN] would symlink {link} -> {dest}")
                    continue
                self._ensure_dir(agent_skills_dir)
                if link.is_symlink():
                    link.unlink()
                elif link.exists():
                    if self.force:
                        shutil.rmtree(link) if link.is_dir() else link.unlink()
                    else:
                        log(
                            f"skipping symlink {link}: already exists (use --force to replace)"
                        )
                        continue
                link.symlink_to(dest)

    # ------------------------------------------------------------------
    # Refresh state initialisation
    # ------------------------------------------------------------------

    def _init_refresh_state(self) -> None:
        """Create ~/.agent/state/shared_asset_refresh_state.json if absent.

        session-start.py checks for this file and fails if it is missing.
        The initial value is an empty object; the refresh mechanism updates it.
        """
        state_path: Path = (
            self.home / ".agent" / "state" / "shared_asset_refresh_state.json"
        )
        if state_path.exists():
            return
        if self.dry_run:
            log(f"[DRY-RUN] would initialize refresh state at {state_path}")
            return
        self._save_json(state_path, {})
        log(f"initialized refresh state at {state_path}")

    # ------------------------------------------------------------------
    # Entry point
    # ------------------------------------------------------------------

    def run(self) -> None:
        """Execute the full installation sequence.

        Creates required directories, copies scripts, installs skills, wires all
        three agents, and initializes the refresh state file.
        """
        # Create all required user-level directories before copying files.
        self._ensure_dir(self.install_root)
        self._ensure_dir(self.skills_root)
        self._ensure_dir(self.home / ".agent" / "state")
        self._ensure_dir(self.home / ".claude")
        self._ensure_dir(self.home / ".codex")
        self._ensure_dir(self.home / ".gemini")

        self._copy_scripts()
        self._install_skills()
        self._init_refresh_state()
        self._wire_agents()
        self._log_agent_support_summary()

        log(f"installed helper files under {self.install_root}")


def main() -> int:
    """Entry point: parse arguments, resolve the repo root, and run the installer.

    Returns:
        int: 0 on success; 1 if the repo root cannot be determined.
    """
    set_runtime_log_context("installer", "n/a")
    parser = argparse.ArgumentParser(
        description="Install agentmemory user assets and wire agent hooks."
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would happen without making changes.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Replace existing installed skill copies rather than skipping them.",
    )
    parser.add_argument(
        "--repo-root",
        help="Override the agentmemory repo root (defaults to git toplevel).",
    )
    args = parser.parse_args()

    if args.repo_root:
        root = Path(args.repo_root).resolve()
    else:
        # Default to the git repo containing this script so the installer can be
        # run from any subdirectory within the agentmemory checkout.
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            print(
                "error: must be run from inside the agentmemory git repo",
                file=sys.stderr,
            )
            return 1
        root = Path(result.stdout.strip()).resolve()

    version = read_version(root)
    print_banner(version)
    Installer(repo_root=root, dry_run=args.dry_run, force=args.force).run()
    log(f"install complete — v{version}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
