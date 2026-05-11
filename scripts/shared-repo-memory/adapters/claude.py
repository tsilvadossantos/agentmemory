#!/usr/bin/env python3
"""claude.py -- Claude Code runtime adapter.

Handles all Claude Code-specific concerns: environment detection, payload
normalization, response rendering, model resolution, installer wiring, and
subagent bootstrap command construction.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from common import find_first
from models import HookRequest, HookResponse, SessionResponse, ShardAttribution

# Claude Code hook event names.
_HOOK_EVENTS = {
    "Stop",
    "SessionStart",
    "SubagentStop",
    "UserPromptSubmit",
    "PostCompact",
}

# Hook events that only Claude Code emits -- used by matches_payload to
# distinguish Claude from Gemini without requiring CLAUDECODE. SessionStart is
# intentionally excluded because all three runtimes emit it; disambiguation for
# that event relies on transcript_path or process ancestry.
_CLAUDE_UNIQUE_HOOK_EVENTS: frozenset[str] = frozenset(
    {"Stop", "SubagentStop", "UserPromptSubmit", "PostCompact"}
)

_HOOK_EVENT_KEYS = {"hook_event_name", "hookEventName"}
_TRANSCRIPT_KEYS = {"transcript_path", "transcriptPath"}

# Payload key aliases -- Claude Code uses camelCase and snake_case variants.
_THREAD_KEYS = {
    "thread_id",
    "threadId",
    "conversation_id",
    "conversationId",
    "session_id",
    "sessionId",
}
_TURN_KEYS = {"turn_id", "turnId", "id"}
_PROMPT_KEYS = {"prompt", "user_prompt", "userPrompt", "inputText", "input_text"}
_ASSISTANT_KEYS = {
    "last_assistant_message",
    "lastAssistantMessage",
    "output_text",
    "summary_text",
    "reasoning_text",
    "prompt_response",
    "text",
    "content",
}


class ClaudeAdapter:
    """Adapter for Claude Code runtime."""

    @staticmethod
    def agent_id() -> str:
        return "claude"

    @staticmethod
    def matches_environment() -> bool:
        return bool(os.environ.get("CLAUDECODE"))

    @staticmethod
    def matches_hook_event(hook_event: str) -> bool:
        return hook_event in _HOOK_EVENTS

    @staticmethod
    def matches_payload(raw: dict[str, Any]) -> bool:
        """Return True when the payload carries Claude-specific markers.

        Claude Code fingerprints: a ``transcript_path`` / ``transcriptPath``
        field (Gemini and Codex do not set one) or a ``hook_event_name`` in the
        Claude-unique set (``Stop``, ``SubagentStop``, ``UserPromptSubmit``,
        ``PostCompact``). SessionStart alone is not sufficient because every
        runtime emits it; that case falls through to process ancestry.
        """
        for key in _TRANSCRIPT_KEYS:
            value = raw.get(key)
            if isinstance(value, str) and value:
                return True
        for key in _HOOK_EVENT_KEYS:
            value = raw.get(key)
            if isinstance(value, str) and value in _CLAUDE_UNIQUE_HOOK_EVENTS:
                return True
        return False

    @staticmethod
    def normalize_hook_request(raw: dict[str, Any]) -> HookRequest:
        return HookRequest(
            hook_event=find_first(raw, {"hook_event_name", "hookEventName"}) or "",
            session_id=find_first(raw, {"session_id", "sessionId"}) or "",
            thread_id=find_first(raw, _THREAD_KEYS) or "",
            turn_id=find_first(raw, _TURN_KEYS) or "",
            cwd=find_first(raw, {"cwd", "workingDirectory"}) or "",
            prompt=find_first(raw, _PROMPT_KEYS) or "",
            assistant_text=find_first(raw, _ASSISTANT_KEYS) or "",
            model=find_first(raw, {"model", "model_name", "modelName"}) or "",
            transcript_path=find_first(raw, {"transcript_path", "transcriptPath"})
            or "",
            raw=raw,
        )

    @staticmethod
    def render_session_response(resp: SessionResponse) -> str:
        payload: dict[str, object] = {"systemMessage": resp.system_message}
        if not resp.continue_session:
            payload["continue"] = False
        if resp.additional_context:
            payload["hookSpecificOutput"] = {
                "hookEventName": "SessionStart",
                "additionalContext": resp.additional_context,
            }
        return json.dumps(payload, sort_keys=True)

    @staticmethod
    def render_hook_response(resp: HookResponse) -> str:
        payload: dict[str, Any] = {"status": resp.status}
        if resp.message:
            payload["message"] = resp.message
        for key, value in resp.extra.items():
            if value is not None:
                payload[key] = value
        return json.dumps(payload, sort_keys=True)

    @staticmethod
    def resolve_model(payload: dict[str, Any]) -> str:
        """Resolve model: CLAUDE_MODEL env var > ~/.claude/settings.json > payload."""
        model = os.environ.get("CLAUDE_MODEL")
        if model:
            return model
        settings_path = Path.home() / ".claude" / "settings.json"
        if settings_path.exists():
            try:
                data = json.loads(settings_path.read_text(encoding="utf-8"))
                model = data.get("model")
                if model:
                    return model
            except (json.JSONDecodeError, OSError):
                pass
        return (
            find_first(payload, {"model", "model_name", "modelName"})
            or "claude-unknown"
        )

    @staticmethod
    def shard_attribution() -> ShardAttribution:
        return ShardAttribution(
            ai_tool="claude",
            ai_surface="claude-code",
            default_model="claude-unknown",
        )

    @staticmethod
    def _build_command(ctx: InstallerContext, script_name: str) -> str:  # noqa: F821
        """Build the hook command string for ``script_name``.

        When ``ctx.python_interpreter`` is set, the command becomes
        ``<python> <script_path>`` so it does not depend on PATH resolution in
        the hook child shell. When unset, the script path is invoked directly
        and relies on its shebang. The script path is always
        ``ctx.install_root / script_name``.
        """
        script_path = ctx.install_root / script_name
        if ctx.python_interpreter is not None:
            return f"{ctx.python_interpreter} {script_path}"
        return str(script_path)

    @staticmethod
    def wire_hooks(ctx: InstallerContext) -> None:  # noqa: F821
        """Wire Claude Code hooks by updating a settings JSON file.

        Writes to ``ctx.settings_path`` when set (per-repo install targets
        ``<repo>/.claude/settings.local.json``); otherwise falls back to the
        user-level ``~/.claude/settings.json`` (global install).
        """

        session_start_cmd = ClaudeAdapter._build_command(ctx, "session-start.py")
        post_turn_cmd = ClaudeAdapter._build_command(ctx, "post-turn-notify.py")
        prompt_guard_cmd = ClaudeAdapter._build_command(ctx, "prompt-guard.py")
        post_compact_cmd = ClaudeAdapter._build_command(ctx, "post-compact.py")

        settings_path = ctx.settings_path or (ctx.home / ".claude" / "settings.json")
        settings = ctx.load_json(settings_path)
        settings["shared_repo_memory_configured"] = True
        settings["shared_agent_assets_repo_path"] = str(ctx.repo_root)

        hooks = settings.setdefault("hooks", {})

        # Each entry: (event_name, command_path, timeout_seconds)
        hook_specs = [
            ("SessionStart", session_start_cmd, 30),
            ("Stop", post_turn_cmd, 60),
            ("SubagentStop", post_turn_cmd, 60),
            ("UserPromptSubmit", prompt_guard_cmd, 10),
            ("PostCompact", post_compact_cmd, 15),
        ]

        for event_name, cmd, timeout in hook_specs:
            event_hooks = hooks.setdefault(event_name, [])
            already_wired = any(
                any(h.get("command") == cmd for h in entry.get("hooks", []))
                for entry in event_hooks
            )
            if not already_wired:
                event_hooks.append(
                    {"hooks": [{"type": "command", "command": cmd, "timeout": timeout}]}
                )

        ctx.save_json(settings_path, settings)

    @staticmethod
    def unwire_hooks(ctx: InstallerContext) -> None:  # noqa: F821
        """Remove this adapter's entries from the targeted settings file.

        Removes only hook entries whose ``command`` contains ``ctx.install_root``
        (covers both the legacy bare-script form and the python-prefixed form),
        and clears the two settings keys the installer set. User-added hooks for
        other tools are preserved. The settings file itself is left in place (it
        may hold unrelated Claude Code settings). Idempotent: safe to run on a
        clean system or repeatedly. Targets ``ctx.settings_path`` when set,
        otherwise the user-level ``~/.claude/settings.json``.
        """
        settings_path = ctx.settings_path or (ctx.home / ".claude" / "settings.json")
        if not settings_path.exists():
            return

        settings = ctx.load_json(settings_path)
        if not settings:
            return

        install_root_str = str(ctx.install_root)
        bool_changed: bool = False

        if settings.get("shared_repo_memory_configured") is True:
            del settings["shared_repo_memory_configured"]
            bool_changed = True
        if isinstance(settings.get("shared_agent_assets_repo_path"), str):
            del settings["shared_agent_assets_repo_path"]
            bool_changed = True

        hooks = settings.get("hooks")
        if isinstance(hooks, dict):
            for event_name in list(hooks.keys()):
                event_list = hooks.get(event_name)
                if not isinstance(event_list, list):
                    continue
                kept_entries: list[dict] = []
                for entry in event_list:
                    if not isinstance(entry, dict):
                        kept_entries.append(entry)
                        continue
                    inner_hooks = entry.get("hooks")
                    if not isinstance(inner_hooks, list):
                        kept_entries.append(entry)
                        continue
                    kept_inner = [
                        h
                        for h in inner_hooks
                        if not (
                            isinstance(h, dict)
                            and isinstance(h.get("command"), str)
                            and install_root_str in h["command"]
                        )
                    ]
                    if len(kept_inner) == len(inner_hooks):
                        kept_entries.append(entry)
                    elif kept_inner:
                        new_entry = dict(entry)
                        new_entry["hooks"] = kept_inner
                        kept_entries.append(new_entry)
                        bool_changed = True
                    else:
                        bool_changed = True
                if kept_entries:
                    hooks[event_name] = kept_entries
                else:
                    del hooks[event_name]
                    bool_changed = True
            if not hooks:
                del settings["hooks"]
                bool_changed = True

        if bool_changed:
            ctx.save_json(settings_path, settings)

    @staticmethod
    def build_bootstrap_command(
        skill_content: str, task: str, repo_root: Path
    ) -> list[str] | None:
        """Build the Claude CLI command used for background bootstrap-style tasks.

        Args:
            skill_content: Full skill text passed as the Claude system prompt.
            task: User-facing task text for the one-shot Claude invocation.
            repo_root: Repository root where the subprocess will be launched.

        Returns:
            list[str] | None: CLI argv for Claude `--print` mode. The command
                intentionally omits any working-directory flag because the
                subprocess caller already sets `cwd`, and current Claude CLI
                releases reject the unsupported `--cwd` option.
        """
        return [
            "claude",
            "-p",
            "--system-prompt",
            skill_content,
            task,
        ]

    @staticmethod
    def timeout_value(seconds: int) -> int:
        return seconds
