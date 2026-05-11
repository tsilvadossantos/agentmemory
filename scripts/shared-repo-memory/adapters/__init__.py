#!/usr/bin/env python3
"""adapters -- Runtime adapter package for the shared repo-memory system.

Each agent runtime (Claude Code, Gemini CLI, Codex CLI) has a concrete adapter
that implements the AgentAdapter protocol.  Core memory logic calls adapter
methods to handle runtime-specific concerns such as:

  - Deterministic runtime detection (payload, process ancestry, env vars)
  - Hook payload normalization into canonical models
  - Response rendering in the runtime's expected JSON schema
  - AI model resolution from env vars and config files
  - Shard attribution (ai_tool, ai_surface)
  - Installer hook wiring
  - Subagent bootstrap command construction

Public API:
  detect_adapter(raw=None)        -- payload -> process tree -> env var -> UnknownAdapter
  detect_adapter_from_hook_event  -- detect from hook_event payload field (post-turn)
  AgentAdapter                    -- typing protocol all adapters implement
  UnknownAdapter                  -- sentinel when detection fails; callers must
                                     check and skip runtime-dependent operations
"""
from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from models import HookRequest, HookResponse, SessionResponse, ShardAttribution


@dataclass
class InstallerContext:
    """Shared state passed to adapter.wire_hooks() during installation.

    Attributes:
        install_root: Directory where the helper scripts referenced by hooks
            live. For a global install this is the canonical copy at
            ~/.agent/shared-repo-memory/; for a per-repo install it is the
            source checkout's scripts/shared-repo-memory/ directory.
        home: User home directory.
        repo_root: Absolute path to the agentmemory source repository root.
        dry_run: When True, log actions without making changes.
        load_json: Callable to load a JSON file, returning {} on missing/corrupt.
        save_json: Callable to persist a dict as pretty-printed JSON.
        settings_path: When set, the adapter writes hook config here instead of
            its default user-level settings file. Used by per-repo install to
            target <target-repo>/.claude/settings.local.json.
        python_interpreter: When set, hook commands are prefixed with this
            absolute interpreter path (e.g. /opt/homebrew/bin/python3.14) so the
            hook is independent of PATH resolution in the child shell. When
            None, the script path is invoked directly, relying on its shebang.
    """

    install_root: Path
    home: Path
    repo_root: Path
    dry_run: bool
    load_json: Any  # Callable[[Path], dict]
    save_json: Any  # Callable[[Path, dict], None]
    settings_path: Path | None = None
    python_interpreter: Path | None = None


@runtime_checkable
class AgentAdapter(Protocol):
    """Protocol that every agent runtime adapter must implement.

    Adapters are stateless; all methods are classmethods or take explicit
    arguments so they can be used without instantiation.
    """

    @staticmethod
    def agent_id() -> str:
        """Return the short machine identifier (e.g., 'claude', 'gemini', 'codex')."""
        ...

    @staticmethod
    def matches_environment() -> bool:
        """Return True when environment variables indicate this runtime is active."""
        ...

    @staticmethod
    def matches_hook_event(hook_event: str) -> bool:
        """Return True when the hook event name belongs to this runtime."""
        ...

    @staticmethod
    def matches_payload(raw: dict[str, Any]) -> bool:
        """Return True when the hook payload carries a fingerprint unique to this runtime.

        Used by detect_adapter() as the strongest deterministic signal. Should
        only return True on high-confidence field signatures; ambiguous events
        (e.g., bare SessionStart with no discriminating fields) must return
        False so detection can fall through to process ancestry.
        """
        ...

    @staticmethod
    def normalize_hook_request(raw: dict[str, Any]) -> HookRequest:
        """Parse a raw stdin JSON dict into a canonical HookRequest."""
        ...

    @staticmethod
    def render_session_response(resp: SessionResponse) -> str:
        """Serialize a SessionResponse to the JSON string expected by this runtime."""
        ...

    @staticmethod
    def render_hook_response(resp: HookResponse) -> str:
        """Serialize a HookResponse to the JSON string expected by this runtime."""
        ...

    @staticmethod
    def resolve_model(payload: dict[str, Any]) -> str:
        """Return the AI model identifier from the payload, env vars, or config files."""
        ...

    @staticmethod
    def shard_attribution() -> ShardAttribution:
        """Return the agent identity fields for shard frontmatter."""
        ...

    @staticmethod
    def wire_hooks(ctx: InstallerContext) -> None:
        """Write agent-specific hook configuration during installation."""
        ...

    @staticmethod
    def unwire_hooks(ctx: InstallerContext) -> None:
        """Reverse wire_hooks: remove only this adapter's entries.

        Must be idempotent: running on a clean system or twice in a row is a
        no-op. Must never remove hook entries added by the user for other tools
        -- identify the adapter's own entries by commands pointing at
        ``ctx.install_root`` or by the canonical hook names this adapter
        writes. Honors ``ctx.dry_run``.
        """
        ...

    @staticmethod
    def build_bootstrap_command(
        skill_content: str, task: str, repo_root: Path
    ) -> list[str] | None:
        """Return the CLI command to spawn a subagent for memory bootstrap.

        Returns None if this runtime cannot spawn subagents.
        """
        ...

    @staticmethod
    def timeout_value(seconds: int) -> int:
        """Convert a timeout in seconds to this runtime's expected unit."""
        ...


# ---- Registry ---------------------------------------------------------------

# Registry order sets the priority used by the payload-match and env-match
# stages of detect_adapter: the first adapter whose matches_payload() /
# matches_environment() returns True wins. Order is Claude, Gemini, Codex --
# when multiple signals collide (rare) this mirrors expected activity levels.
# UnknownAdapter is NOT registered here; it is returned by detect_adapter only
# when every stage (payload, process tree, env) fails to identify a runtime.
from adapters.claude import ClaudeAdapter  # noqa: E402
from adapters.codex import CodexAdapter  # noqa: E402
from adapters.gemini import GeminiAdapter  # noqa: E402

_ADAPTERS: list[type[AgentAdapter]] = [ClaudeAdapter, GeminiAdapter, CodexAdapter]

# Binary names that identify an agent runtime process. Used by the
# process-tree detection helper to resolve the active runtime even when the
# runtime did not export its well-known env var to the hook subprocess.
_RUNTIME_BINARIES: frozenset[str] = frozenset({"claude", "gemini", "codex"})

# Maximum ancestor depth walked when looking for a runtime binary. Bounded so a
# pathological ancestry chain cannot hang SessionStart.
_PROCESS_TREE_MAX_DEPTH: int = 6

# Per-invocation timeout for `ps`. Healthy systems answer in <10ms; the cap
# exists to bound a pathological freeze. With _PROCESS_TREE_MAX_DEPTH=6 this
# keeps the total detection budget under 6s, well inside SessionStart's 30s
# hook timeout.
_PS_TIMEOUT_SECONDS: float = 1.0


def _parent_comm(int_pid: int) -> tuple[int, str] | None:
    """Return (ppid, comm) for the given pid using ``ps``.

    Args:
        int_pid: Process id whose parent id and command name to look up.

    Returns:
        tuple[int, str] | None: (parent_pid, command_basename) pair, or None
        when ``ps`` is unavailable (e.g., Windows, restricted sandbox) or the
        pid has no entry.
    """
    try:
        process_result: subprocess.CompletedProcess[str] = subprocess.run(
            ["ps", "-o", "ppid=,comm=", "-p", str(int_pid)],
            capture_output=True,
            text=True,
            timeout=_PS_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    str_line: str = process_result.stdout.strip()
    if not str_line:
        return None
    list_str_parts: list[str] = str_line.split(None, 1)
    if len(list_str_parts) != 2:
        return None
    try:
        int_ppid: int = int(list_str_parts[0])
    except ValueError:
        return None
    str_comm: str = Path(list_str_parts[1].strip()).name
    return (int_ppid, str_comm)


@cache
def _detect_runtime_from_process_tree() -> str | None:
    """Walk the process ancestry looking for a known runtime binary.

    A hook subprocess is always a descendant of its runtime CLI
    (``claude``, ``gemini``, or ``codex``), so the ancestor chain is the most
    deterministic detection signal: it does not require the runtime to export
    a discovery env var, and it works identically for every hook event.

    Returns:
        str | None: ``"claude"``, ``"gemini"``, or ``"codex"`` when a matching
        binary name appears within ``_PROCESS_TREE_MAX_DEPTH`` levels above the
        current process; None when no match is found or ``ps`` is unavailable.
    """
    int_pid: int = os.getppid()
    for _ in range(_PROCESS_TREE_MAX_DEPTH):
        tuple_parent: tuple[int, str] | None = _parent_comm(int_pid)
        if tuple_parent is None:
            return None
        int_ppid, str_comm = tuple_parent
        if str_comm in _RUNTIME_BINARIES:
            return str_comm
        if int_ppid == 0 or int_ppid == int_pid:
            return None
        int_pid = int_ppid
    return None


def _runtime_id_to_adapter(str_runtime_id: str) -> type[AgentAdapter] | None:
    """Map a runtime identifier string to its adapter class.

    Args:
        str_runtime_id: Short identifier such as ``"claude"``.

    Returns:
        type[AgentAdapter] | None: Adapter class for the runtime, or None when
        the identifier is unrecognized.
    """
    for adapter in _ADAPTERS:
        if adapter.agent_id() == str_runtime_id:
            return adapter
    return None


class UnknownAdapter:
    """Sentinel adapter returned when runtime detection cannot resolve a runtime.

    Callers that spawn subagents, tag log lines, or write shard attribution MUST
    check for this adapter (via ``agent_id() == "unknown"`` or ``is UnknownAdapter``)
    and either skip runtime-dependent work or log a precise diagnostic. Silently
    treating an unknown runtime as a default (previously Codex) produces
    misattributed logs and disables bootstrap in client repos running Claude
    Code when the ``CLAUDECODE`` env var fails to propagate.

    Rendering methods delegate to a neutral Claude-compatible shape so any
    error response that does reach a runtime remains valid JSON.
    """

    @staticmethod
    def agent_id() -> str:
        return "unknown"

    @staticmethod
    def matches_environment() -> bool:
        return False

    @staticmethod
    def matches_hook_event(hook_event: str) -> bool:
        return False

    @staticmethod
    def matches_payload(raw: dict[str, Any]) -> bool:
        return False

    @staticmethod
    def normalize_hook_request(raw: dict[str, Any]) -> HookRequest:
        return ClaudeAdapter.normalize_hook_request(raw)

    @staticmethod
    def render_session_response(resp: SessionResponse) -> str:
        return ClaudeAdapter.render_session_response(resp)

    @staticmethod
    def render_hook_response(resp: HookResponse) -> str:
        return ClaudeAdapter.render_hook_response(resp)

    @staticmethod
    def resolve_model(payload: dict[str, Any]) -> str:
        return "unknown"

    @staticmethod
    def shard_attribution() -> ShardAttribution:
        return ShardAttribution(
            ai_tool="unknown",
            ai_surface="unknown",
            default_model="unknown",
        )

    @staticmethod
    def wire_hooks(ctx: InstallerContext) -> None:
        return None

    @staticmethod
    def unwire_hooks(ctx: InstallerContext) -> None:
        return None

    @staticmethod
    def build_bootstrap_command(
        skill_content: str, task: str, repo_root: Path
    ) -> list[str] | None:
        return None

    @staticmethod
    def timeout_value(seconds: int) -> int:
        return seconds


def detect_adapter(raw: dict[str, Any] | None = None) -> type[AgentAdapter]:
    """Detect the active runtime using layered deterministic signals.

    Resolution order, most-to-least deterministic:

      1. **Payload fingerprint.** When a hook payload is available, the first
         adapter whose ``matches_payload(raw)`` returns True wins. Adapters
         only return True for high-confidence markers unique to their runtime.
      2. **Process ancestry.** Walk the ppid chain for a ``claude``, ``gemini``,
         or ``codex`` binary name. A hook subprocess is always a descendant of
         its runtime CLI, so this signal is independent of env var propagation.
      3. **Environment variables.** Legacy fallback for CI/test contexts and
         for runtimes that set process-wide discovery env vars.
      4. **UnknownAdapter sentinel.** Detection failed. Callers must handle
         this explicitly rather than silently treating an unknown runtime as
         any specific runtime.

    Args:
        raw: Optional parsed hook payload. When provided, payload-based
            detection runs first; otherwise resolution begins with process
            ancestry.

    Returns:
        type[AgentAdapter]: The resolved adapter class, or ``UnknownAdapter``
        when no signal identifies a runtime.
    """
    if raw is not None:
        for adapter in _ADAPTERS:
            if adapter.matches_payload(raw):
                return adapter
    str_runtime_from_tree: str | None = _detect_runtime_from_process_tree()
    if str_runtime_from_tree is not None:
        adapter_from_tree: type[AgentAdapter] | None = _runtime_id_to_adapter(
            str_runtime_from_tree
        )
        if adapter_from_tree is not None:
            return adapter_from_tree
    for adapter in _ADAPTERS:
        if adapter.matches_environment():
            return adapter
    return UnknownAdapter


def detect_adapter_from_hook_event(
    hook_event: str, raw: dict[str, Any] | None = None
) -> type[AgentAdapter]:
    """Detect the active runtime from the hook event name in the payload.

    Used primarily by post-turn-notify.py where the hook_event field is the
    most reliable signal (e.g., ``Stop`` for Claude, ``AfterAgent`` for Gemini).
    Falls back to the full layered ``detect_adapter`` when no adapter claims
    the event.

    Args:
        hook_event: Hook event name from the payload (possibly empty).
        raw: Optional full payload passed through to the fallback detection.

    Returns:
        type[AgentAdapter]: The resolved adapter class.
    """
    for adapter in _ADAPTERS:
        if adapter.matches_hook_event(hook_event):
            return adapter
    return detect_adapter(raw)


__all__ = [
    "AgentAdapter",
    "InstallerContext",
    "ClaudeAdapter",
    "GeminiAdapter",
    "CodexAdapter",
    "UnknownAdapter",
    "detect_adapter",
    "detect_adapter_from_hook_event",
]
