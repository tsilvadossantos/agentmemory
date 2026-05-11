#!/usr/bin/env python3
"""uninstall.py -- Reverse the shared-repo-memory installer.

Run via the repo root entry point:
    ./uninstall.sh [--repo] [--purge-memory] [--dry-run]

Or directly:
    python3 scripts/shared-repo-memory/uninstall.py [--repo] [--purge-memory] [--dry-run]

Scopes:
  Global (default): reverse the user-level install performed by install.sh.
    - Unwire Claude Code / Gemini CLI / Codex CLI hooks (each adapter removes
      only its own entries; user-added hooks for other tools are preserved).
    - Remove the per-agent skill symlinks we created under
      ``~/.claude/skills/``, ``~/.codex/skills/``, ``~/.gemini/skills/``.
    - Remove canonical skill copies under ``~/.agent/skills/<skill>/`` for
      skills the installer ships.
    - Remove the installed helper scripts at ``~/.agent/shared-repo-memory/``.
    - Remove ``~/.agent/state/shared_asset_refresh_state.json``.

  Per-repo (``--repo``): reverse the per-repo wiring created by
    bootstrap-repo.py in the current repository:
    - Remove the installer's canonical git hooks under ``.githooks/`` only
      when their content still matches exactly (never clobber user edits).
    - Unset ``core.hooksPath`` only when it still equals ``.githooks`` and the
      directory has no other hooks remaining.
    - Remove the ``.codex/memory`` symlink.
    - Remove empty ``.claude/local/`` and ``.codex/local/`` scratch dirs.
    - Strip the installer's ``.gitignore`` marker block.
    - Leave ``.agents/memory/`` alone unless ``--purge-memory`` is also set.

``--purge-memory`` (per-repo only): stage ``git rm -r .agents/memory`` so the
committed memory history is removed on the next commit. The operator reviews
and commits explicitly; this command does not commit.

``--dry-run`` prints every action without mutating state at either scope.

After a global uninstall, restart any open agent sessions so the unwired hook
config takes effect. The SessionStart hook will no longer fire.
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

from adapters import ClaudeAdapter, CodexAdapter, GeminiAdapter, InstallerContext
from common import (
    GITHOOKS_RELATIVE_DIR,
    REQUIRED_GITIGNORE_ENTRIES,
    format_log_prefix,
    set_runtime_log_context,
)

# Adapter order for uninstallation. Each adapter removes only its own entries,
# so the order is cosmetic (affects log output sequence).
_ADAPTERS = [ClaudeAdapter, CodexAdapter, GeminiAdapter]

# Per-agent directories where the installer placed skill symlinks.
_AGENT_SKILL_DIRS: tuple[str, ...] = (
    ".claude/skills",
    ".codex/skills",
    ".gemini/skills",
)


def log(message: str) -> None:
    """Print an uninstaller log line with runtime metadata."""
    print(f"{format_log_prefix()} {message}")


def _load_json_safe(path: Path) -> dict:
    """Load a JSON file, returning {} on any error."""
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def _save_json_pretty(path: Path, data: dict) -> None:
    """Write data as pretty-printed JSON, creating parents if needed."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


class GlobalUninstaller:
    """Reverse the user-level install performed by ``install.py``.

    Attributes:
        home: User home directory (defaulted to ``Path.home()``).
        dry_run: When True, log actions without mutating state.
        repo_root: Absolute path to the agentmemory authoring repository, used
            only to pass into adapter ``unwire_hooks`` so adapters can match
            the project-scoped entries they originally wrote.
        skills_to_remove: Names of skill directories the installer shipped.
            Loaded from ``repo_root/skills/`` if available; otherwise empty so
            no skill directory is touched.
    """

    def __init__(
        self,
        *,
        repo_root: Path,
        home: Path,
        dry_run: bool,
    ) -> None:
        self.home = home
        self.dry_run = dry_run
        self.repo_root = repo_root
        self.install_root = home / ".agent" / "shared-repo-memory"
        self.skills_root = home / ".agent" / "skills"
        self.refresh_state_path = (
            home / ".agent" / "state" / "shared_asset_refresh_state.json"
        )
        self.skills_to_remove: set[str] = set()
        skills_src: Path = repo_root / "skills"
        if skills_src.is_dir():
            self.skills_to_remove = {
                p.name for p in skills_src.iterdir() if p.is_dir()
            }

    def _remove_path(self, path: Path) -> None:
        """Remove ``path`` if it exists, honoring ``dry_run``.

        Handles files, directories, and symlinks. Silently ignores paths that
        do not exist (idempotent).
        """
        if not path.exists() and not path.is_symlink():
            return
        if self.dry_run:
            log(f"[DRY-RUN] would remove {path}")
            return
        if path.is_symlink() or path.is_file():
            path.unlink()
        elif path.is_dir():
            shutil.rmtree(path)
        log(f"removed {path}")

    def _unwire_all_agents(self) -> None:
        """Dispatch to each adapter's ``unwire_hooks`` in order.

        The save_json callable short-circuits in dry-run mode so Claude and
        Gemini adapters (which delegate dry-run enforcement to save_json)
        cannot accidentally mutate real state. The Codex adapter also writes
        config.toml directly and guards its own dry-run early return.
        """
        def save_json(path: Path, payload: dict) -> None:
            if self.dry_run:
                log(f"[DRY-RUN] would write {path}")
                return
            _save_json_pretty(path, payload)

        ctx = InstallerContext(
            install_root=self.install_root,
            home=self.home,
            repo_root=self.repo_root,
            dry_run=self.dry_run,
            load_json=_load_json_safe,
            save_json=save_json,
        )
        for adapter in _ADAPTERS:
            adapter.unwire_hooks(ctx)
            if self.dry_run:
                log(f"[DRY-RUN] would unwire {adapter.agent_id()} hooks")
            else:
                log(f"unwired {adapter.agent_id()} hooks")

    def _remove_skill_symlinks(self) -> None:
        """Remove the per-agent skill symlinks the installer created.

        Only removes symlinks that resolve into ``self.skills_root``; any
        hand-placed directory or a symlink pointing somewhere else is
        preserved.
        """
        for str_agent_dir in _AGENT_SKILL_DIRS:
            agent_skills: Path = self.home / str_agent_dir
            if not agent_skills.exists():
                continue
            for str_skill_name in self.skills_to_remove:
                link: Path = agent_skills / str_skill_name
                if not link.exists() and not link.is_symlink():
                    continue
                if link.is_symlink():
                    try:
                        resolved: Path = link.resolve()
                    except OSError:
                        resolved = link
                    if self.skills_root in resolved.parents or resolved == (
                        self.skills_root / str_skill_name
                    ):
                        self._remove_path(link)
                    else:
                        # Symlink points somewhere unrelated; leave it alone.
                        log(f"skipping {link}: symlink target outside ~/.agent/skills")
                # Non-symlink entries are left alone; they are not ours.

    def _remove_shipped_skills(self) -> None:
        """Remove the canonical skill copies the installer placed under ``~/.agent/skills``."""
        for str_skill_name in self.skills_to_remove:
            skill_path: Path = self.skills_root / str_skill_name
            if skill_path.exists() or skill_path.is_symlink():
                self._remove_path(skill_path)
        # Remove ~/.agent/skills if it is now empty.
        if self.skills_root.is_dir() and not any(self.skills_root.iterdir()):
            self._remove_path(self.skills_root)

    def _remove_install_root(self) -> None:
        """Remove ``~/.agent/shared-repo-memory/`` entirely."""
        if self.install_root.exists():
            self._remove_path(self.install_root)

    def _remove_refresh_state(self) -> None:
        """Remove the refresh-state JSON the installer seeded."""
        if self.refresh_state_path.exists():
            self._remove_path(self.refresh_state_path)
        # Remove ~/.agent/state if it is now empty.
        state_dir: Path = self.refresh_state_path.parent
        if state_dir.is_dir() and not any(state_dir.iterdir()):
            self._remove_path(state_dir)

    def run(self) -> None:
        """Execute the full global uninstall sequence."""
        self._unwire_all_agents()
        self._remove_skill_symlinks()
        self._remove_shipped_skills()
        self._remove_install_root()
        self._remove_refresh_state()


class RepoUninstaller:
    """Reverse the per-repo wiring placed by ``bootstrap-repo.py``.

    Attributes:
        repo_root: Absolute path to the repository being unwired.
        dry_run: When True, log actions without mutating state.
        purge_memory: When True, stage ``git rm -r .agents/memory``. The caller
            is expected to review and commit the staged deletion explicitly.
    """

    _HOOK_NAMES: tuple[str, ...] = (
        "pre-commit",
        "post-checkout",
        "post-merge",
        "post-rewrite",
    )

    def __init__(
        self,
        *,
        repo_root: Path,
        dry_run: bool,
        purge_memory: bool,
    ) -> None:
        self.repo_root = repo_root
        self.dry_run = dry_run
        self.purge_memory = purge_memory
        self.hooks_dir: Path = repo_root / GITHOOKS_RELATIVE_DIR

    def _remove_path(self, path: Path) -> None:
        """Remove ``path`` if it exists (files, dirs, symlinks)."""
        if not path.exists() and not path.is_symlink():
            return
        if self.dry_run:
            log(f"[DRY-RUN] would remove {path}")
            return
        if path.is_symlink() or path.is_file():
            path.unlink()
        elif path.is_dir():
            shutil.rmtree(path)
        log(f"removed {path}")

    def _remove_canonical_git_hooks(self) -> None:
        """Remove hook scripts whose content matches what bootstrap-repo wrote.

        Loads the canonical hook text from ``bootstrap-repo.py`` via direct
        import and compares byte-for-byte with each installed hook. User edits
        are preserved (the hook is left alone when content differs).
        """
        try:
            # Local import keeps the dependency contained -- bootstrap-repo
            # lives in the same directory as this script post-install, and in
            # the source tree during tests.
            sys.path.insert(0, str(Path(__file__).resolve().parent))
            import importlib

            bootstrap_module = importlib.import_module("bootstrap_repo_module")
        except ImportError:
            # Fall back to reading the bootstrap-repo.py source for git_hook_text.
            bootstrap_module = self._load_bootstrap_as_module()

        if bootstrap_module is None:
            log(
                "cannot locate bootstrap-repo.py; skipping canonical-hook removal "
                "(remove .githooks/ manually if desired)"
            )
            return

        for str_hook in self._HOOK_NAMES:
            hook_path: Path = self.hooks_dir / str_hook
            if not hook_path.exists():
                continue
            try:
                str_current: str = hook_path.read_text(encoding="utf-8")
            except OSError:
                continue
            str_expected: str = bootstrap_module.git_hook_text(str_hook)
            if str_current == str_expected:
                self._remove_path(hook_path)
            else:
                log(
                    f"preserving .githooks/{str_hook}: content differs from "
                    "installer canonical form (user edit suspected)"
                )

    def _load_bootstrap_as_module(self):
        """Dynamically load bootstrap-repo.py so we can reuse ``git_hook_text``.

        The source filename contains a hyphen which prevents a plain import,
        so we use importlib.util to load it as a module.
        """
        import importlib.util

        script_dir: Path = Path(__file__).resolve().parent
        source_path: Path = script_dir / "bootstrap-repo.py"
        if not source_path.exists():
            return None
        spec = importlib.util.spec_from_file_location(
            "_bootstrap_repo_for_uninstall", source_path
        )
        if spec is None or spec.loader is None:
            return None
        module = importlib.util.module_from_spec(spec)
        try:
            spec.loader.exec_module(module)
        except Exception:
            return None
        return module

    def _unset_git_hooks_path(self) -> None:
        """Unset ``core.hooksPath`` when it still points at the installer's dir.

        Only unsets the config when the value is literally ``.githooks`` and
        the directory is empty (all canonical hooks removed above, no user
        hooks remaining). Preserves user-managed hook paths.
        """
        result = subprocess.run(
            ["git", "config", "--get", "core.hooksPath"],
            cwd=str(self.repo_root),
            check=False,
            capture_output=True,
            text=True,
        )
        str_current: str = result.stdout.strip()
        if str_current != GITHOOKS_RELATIVE_DIR:
            return
        if self.hooks_dir.exists() and any(self.hooks_dir.iterdir()):
            log(
                "preserving git config core.hooksPath: .githooks/ still contains "
                "files (user hooks suspected)"
            )
            return
        if self.dry_run:
            log("[DRY-RUN] would unset git config core.hooksPath")
            return
        subprocess.run(
            ["git", "config", "--unset", "core.hooksPath"],
            cwd=str(self.repo_root),
            check=False,
        )
        log("unset git config core.hooksPath")

    def _remove_hooks_dir_if_empty(self) -> None:
        """Remove ``.githooks/`` when it has no remaining entries."""
        if not self.hooks_dir.is_dir():
            return
        if any(self.hooks_dir.iterdir()):
            return
        self._remove_path(self.hooks_dir)

    def _remove_codex_memory_symlink(self) -> None:
        """Remove the ``.codex/memory`` symlink when present."""
        link: Path = self.repo_root / ".codex" / "memory"
        if link.is_symlink():
            self._remove_path(link)

    def _remove_empty_local_dirs(self) -> None:
        """Remove ``.claude/local/`` and ``.codex/local/`` when empty.

        These are scratch directories. Leaves them alone when the operator has
        put anything inside.
        """
        for str_rel in (".claude/local", ".codex/local"):
            candidate: Path = self.repo_root / str_rel
            if candidate.is_dir() and not any(candidate.iterdir()):
                self._remove_path(candidate)

    def _strip_gitignore_block(self) -> None:
        """Remove the installer's marker block from ``.gitignore``.

        Matches from the opening marker comment
        ``# agentmemory-managed local repo wiring and state`` through the last
        canonical entry the installer appends. Never touches content outside
        that block.
        """
        gitignore_path: Path = self.repo_root / ".gitignore"
        if not gitignore_path.exists():
            return
        try:
            str_text: str = gitignore_path.read_text(encoding="utf-8")
        except OSError:
            return

        # Walk lines starting at the installer's header comment and remove
        # every contiguous line that belongs to the canonical entry set. This
        # tolerates subsets of REQUIRED_GITIGNORE_ENTRIES (e.g. a per-repo
        # Claude-only install writes the block without ``.codex/local/``) while
        # never touching user-added lines.
        canonical: set[str] = set(REQUIRED_GITIGNORE_ENTRIES)
        list_str_lines: list[str] = str_text.splitlines()
        header_line: str = REQUIRED_GITIGNORE_ENTRIES[0]
        try:
            int_start: int = list_str_lines.index(header_line)
        except ValueError:
            return
        int_end: int = int_start
        while int_end < len(list_str_lines) and list_str_lines[int_end] in canonical:
            int_end += 1

        if self.dry_run:
            log(f"[DRY-RUN] would strip installer block from {gitignore_path}")
            return

        # Also swallow a single blank line directly above the block, if any,
        # so the removal does not leave a stray gap.
        int_removal_start: int = int_start
        if int_start > 0 and list_str_lines[int_start - 1] == "":
            int_removal_start = int_start - 1

        list_str_kept: list[str] = (
            list_str_lines[:int_removal_start] + list_str_lines[int_end:]
        )
        str_stripped: str = "\n".join(list_str_kept)
        str_stripped = re.sub(r"\n{3,}", "\n\n", str_stripped)
        str_stripped = str_stripped.rstrip("\n")
        if str_stripped:
            str_stripped += "\n"
        gitignore_path.write_text(str_stripped, encoding="utf-8")
        log(f"stripped installer .gitignore block from {gitignore_path}")

    def _stage_memory_purge(self) -> None:
        """Run ``git rm -r`` against ``.agents/memory`` (does not commit).

        The operator explicitly opts in with ``--purge-memory``. We stage the
        deletion so the operator can review and commit. We do not drop the
        working-tree directory ourselves because it may contain unstaged
        local work the operator wants to keep.
        """
        memory_dir: Path = self.repo_root / ".agents" / "memory"
        if not memory_dir.exists():
            return
        if self.dry_run:
            log(f"[DRY-RUN] would run git rm -r --cached {memory_dir}")
            return
        # ``--cached`` stages deletion of tracked files without removing the
        # working-tree copy. Operator can then manually review and commit.
        subprocess.run(
            ["git", "rm", "-r", "--cached", str(memory_dir.relative_to(self.repo_root))],
            cwd=str(self.repo_root),
            check=False,
        )
        log(
            f"staged deletion of {memory_dir} via git rm --cached; "
            "review and commit explicitly"
        )

    def _unwire_claude_settings_local(self) -> None:
        """Remove per-repo Claude hook entries from settings.local.json.

        Mirrors what RepoInstaller wires. Matches by the scripts dir of the
        agentmemory source checkout recorded in
        ``shared_agent_assets_repo_path`` so we never remove hooks that point
        at a different source checkout.
        """
        settings_path: Path = self.repo_root / ".claude" / "settings.local.json"
        if not settings_path.exists():
            return

        settings = _load_json_safe(settings_path)
        if not settings:
            return

        # Use the recorded source-checkout path to derive install_root. Fall
        # back to a substring of "/scripts/shared-repo-memory/" which is
        # uniquely the per-repo install signature.
        source_repo_path = settings.get("shared_agent_assets_repo_path")
        if isinstance(source_repo_path, str) and source_repo_path:
            install_root_str = str(
                Path(source_repo_path) / "scripts" / "shared-repo-memory"
            )
        else:
            install_root_str = "/scripts/shared-repo-memory"

        def save_json(path: Path, payload: dict) -> None:
            if self.dry_run:
                log(f"[DRY-RUN] would write {path}")
                return
            _save_json_pretty(path, payload)

        ctx = InstallerContext(
            install_root=Path(install_root_str),
            home=Path.home(),
            repo_root=self.repo_root,
            dry_run=self.dry_run,
            load_json=_load_json_safe,
            save_json=save_json,
            settings_path=settings_path,
        )
        ClaudeAdapter.unwire_hooks(ctx)
        if self.dry_run:
            log(f"[DRY-RUN] would unwire claude hooks in {settings_path}")
        else:
            log(f"unwired claude hooks in {settings_path}")

    def run(self) -> None:
        """Execute the full per-repo uninstall sequence."""
        self._unwire_claude_settings_local()
        self._remove_canonical_git_hooks()
        self._unset_git_hooks_path()
        self._remove_hooks_dir_if_empty()
        self._remove_codex_memory_symlink()
        self._remove_empty_local_dirs()
        self._strip_gitignore_block()
        if self.purge_memory:
            self._stage_memory_purge()


def _resolve_repo_root(str_override: str | None) -> Path | None:
    """Resolve the repository root from an explicit override or ``git``.

    Returns None when ``str_override`` is absent and the current directory is
    not inside a git repo.
    """
    if str_override:
        return Path(str_override).resolve()
    result = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return None
    return Path(result.stdout.strip()).resolve()


def main() -> int:
    """Parse arguments and dispatch to the selected uninstaller.

    Returns:
        int: 0 on success; 1 on misconfigured flags or missing repo context.
    """
    set_runtime_log_context("installer", "n/a")
    parser = argparse.ArgumentParser(
        description="Uninstall agentmemory user assets and/or per-repo wiring."
    )
    parser.add_argument(
        "--repo",
        action="store_true",
        help="Per-repo uninstall (run from inside a bootstrapped repo).",
    )
    parser.add_argument(
        "--purge-memory",
        action="store_true",
        help="With --repo, stage deletion of .agents/memory via git rm --cached.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print every action without making any changes.",
    )
    parser.add_argument(
        "--repo-root",
        help="Override the repository root (defaults to git toplevel).",
    )
    args = parser.parse_args()

    if args.purge_memory and not args.repo:
        print(
            "error: --purge-memory only applies to --repo uninstall",
            file=sys.stderr,
        )
        return 1

    if args.repo:
        repo_root: Path | None = _resolve_repo_root(args.repo_root)
        if repo_root is None:
            print(
                "error: --repo requires running inside a git repository",
                file=sys.stderr,
            )
            return 1
        RepoUninstaller(
            repo_root=repo_root,
            dry_run=args.dry_run,
            purge_memory=args.purge_memory,
        ).run()
        log(f"per-repo uninstall complete{' (dry run)' if args.dry_run else ''}")
        return 0

    # Global uninstall. We still need a repo_root for adapter context, but
    # only for identifying project-scoped entries; fall back to the current
    # directory if git is unavailable.
    repo_root_global: Path = (
        _resolve_repo_root(args.repo_root) or Path.cwd().resolve()
    )
    GlobalUninstaller(
        repo_root=repo_root_global,
        home=Path.home(),
        dry_run=args.dry_run,
    ).run()
    log(f"global uninstall complete{' (dry run)' if args.dry_run else ''}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
