#!/usr/bin/env python3
"""cursor.py -- Cursor Agent CLI runtime adapter.

Handles all Cursor Agent CLI-specific concerns: environment detection, payload
normalization, response rendering, model resolution, installer wiring, and
subagent bootstrap command construction.

The binary is invoked as ``agent`` (not ``cursor`` or ``cursor-agent``), so
``agent_id()`` returns ``"agent"`` to align with the process-tree binary name
and the ai-skills slug.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from common import find_first
from models import HookRequest, HookResponse, SessionResponse, ShardAttribution

# Cursor Agent hook event names (camelCase, distinct from Claude's PascalCase).
_HOOK_EVENTS = {
    "sessionStart",
    "sessionEnd",
    "preToolUse",
    "postToolUse",
    "postToolUseFailure",
    "subagentStart",
    "subagentStop",
    "beforeShellExecution",
    "afterShellExecution",
    "beforeMCPExecution",
    "afterMCPExecution",
    "beforeReadFile",
    "afterFileEdit",
    "beforeSubmitPrompt",
    "preCompact",
    "stop",
    "afterAgentResponse",
    "afterAgentThought",
}

_HOOK_EVENT_KEYS = {"hook_event_name", "hookEventName"}

_THREAD_KEYS = {
    "thread_id",
    "threadId",
    "conversation_id",
    "conversationId",
    "session_id",
    "sessionId",
}
_TURN_KEYS = {"turn_id", "turnId", "generation_id", "generationId", "id"}
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


class CursorAdapter:
    """Adapter for Cursor Agent CLI runtime."""

    @staticmethod
    def agent_id() -> str:
        return "agent"

    @staticmethod
    def matches_environment() -> bool:
        # CURSOR_VERSION is set automatically by the Cursor Agent CLI in every
        # hook subprocess. CURSOR_PROJECT_DIR is a secondary fallback.
        return bool(os.environ.get("CURSOR_VERSION")) or bool(
            os.environ.get("CURSOR_PROJECT_DIR")
        )

    @staticmethod
    def matches_hook_event(hook_event: str) -> bool:
        return hook_event in _HOOK_EVENTS

    @staticmethod
    def matches_payload(raw: dict[str, Any]) -> bool:
        """Return True when the payload carries Cursor-specific markers.

        ``cursor_version`` is unique to Cursor and present in every hook
        payload except ``workspaceOpen``. ``generation_id`` is a Cursor-only
        field that appears on agent-session hooks, used as a secondary signal.
        """
        if isinstance(raw.get("cursor_version"), str):
            return True
        if isinstance(raw.get("generation_id"), str):
            return True
        if isinstance(raw.get("workspace_roots"), list):
            return True
        return False

    @staticmethod
    def normalize_hook_request(raw: dict[str, Any]) -> HookRequest:
        return HookRequest(
            hook_event=find_first(raw, _HOOK_EVENT_KEYS) or "",
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
        # Cursor's sessionStart hook reads systemMessage / additionalContext
        # from a hookSpecificOutput block, mirroring Claude's shape closely
        # enough that the same payload structure works.
        payload: dict[str, object] = {"systemMessage": resp.system_message}
        if not resp.continue_session:
            payload["continue"] = False
        if resp.additional_context:
            payload["hookSpecificOutput"] = {
                "hookEventName": "sessionStart",
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
        model = find_first(payload, {"model", "model_name", "modelName"})
        return model or "cursor-unknown"

    @staticmethod
    def shard_attribution() -> ShardAttribution:
        return ShardAttribution(
            ai_tool="cursor",
            ai_surface="cursor-agent",
            default_model="cursor-unknown",
        )

    @staticmethod
    def wire_hooks(ctx: "InstallerContext") -> None:  # noqa: F821
        """Wire Cursor Agent hooks by updating ~/.cursor/hooks.json.

        Cursor hooks.json is a standalone file (not inside a larger settings
        file) with shape: ``{"version": 1, "hooks": {<event>: [<entry>, ...]}}``.
        Each entry is a flat dict with ``command`` plus optional ``type``,
        ``timeout``, ``matcher``, ``loop_limit``, ``failClosed``. Timeouts are
        in seconds.
        """
        session_start_cmd = str(ctx.install_root / "session-start.py")
        post_turn_cmd = str(ctx.install_root / "post-turn-notify.py")
        prompt_guard_cmd = str(ctx.install_root / "prompt-guard.py")
        post_compact_cmd = str(ctx.install_root / "post-compact.py")

        hooks_path = ctx.home / ".cursor" / "hooks.json"
        settings = ctx.load_json(hooks_path)
        settings.setdefault("version", 1)
        settings["shared_repo_memory_configured"] = True
        settings["shared_agent_assets_repo_path"] = str(ctx.repo_root)

        hooks = settings.setdefault("hooks", {})

        # (event_name, command_path, timeout_seconds)
        hook_specs = [
            ("sessionStart", session_start_cmd, 30),
            ("stop", post_turn_cmd, 60),
            ("subagentStop", post_turn_cmd, 60),
            ("beforeSubmitPrompt", prompt_guard_cmd, 10),
            ("preCompact", post_compact_cmd, 15),
        ]

        for event_name, cmd, timeout in hook_specs:
            event_list = hooks.setdefault(event_name, [])
            already_wired = any(
                isinstance(h, dict) and h.get("command") == cmd
                for h in event_list
            )
            if not already_wired:
                event_list.append(
                    {"command": cmd, "type": "command", "timeout": timeout}
                )

        ctx.save_json(hooks_path, settings)

    @staticmethod
    def unwire_hooks(ctx: "InstallerContext") -> None:  # noqa: F821
        """Remove this adapter's entries from ``~/.cursor/hooks.json``.

        Removes only hook entries whose ``command`` path starts with
        ``ctx.install_root``, and clears the two settings keys the installer
        set. User-added hooks are preserved. Idempotent.
        """
        hooks_path = ctx.home / ".cursor" / "hooks.json"
        if not hooks_path.exists():
            return

        settings = ctx.load_json(hooks_path)
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
                kept = [
                    h
                    for h in event_list
                    if not (
                        isinstance(h, dict)
                        and isinstance(h.get("command"), str)
                        and h["command"].startswith(install_root_str)
                    )
                ]
                if len(kept) == len(event_list):
                    continue
                if kept:
                    hooks[event_name] = kept
                else:
                    del hooks[event_name]
                bool_changed = True
            if not hooks:
                del settings["hooks"]
                bool_changed = True

        if bool_changed:
            ctx.save_json(hooks_path, settings)

    @staticmethod
    def build_bootstrap_command(
        skill_content: str, task: str, repo_root: Path
    ) -> list[str] | None:
        # Cursor Agent CLI subagent-spawn flags are not stable across versions.
        # Return None so memory-bootstrap callers skip Cursor cleanly; revisit
        # once the CLI exposes a documented one-shot invocation.
        return None

    @staticmethod
    def timeout_value(seconds: int) -> int:
        # Cursor hook timeouts are in seconds, same as Claude / Codex.
        return seconds
