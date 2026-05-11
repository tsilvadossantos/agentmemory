#!/usr/bin/env python3
"""auto-bootstrap.py -- Background semantic bootstrap for the shared repo-memory system.

Spawned as a detached subprocess by session-start.py when a wired repo has no
event shards yet.  Runs independently of the agent session -- the hook returns
immediately and this process completes in the background.

What it does
------------
1. Collects repo context: recent git log (last 24 commits) and design-like docs.
2. Calls the Anthropic API (messages endpoint) with a prompt derived from the
   memory-bootstrap skill instructions, requesting structured JSON output.
3. Parses the JSON response and writes event shards and ADRs to disk.
4. Calls rebuild-summary.py for each affected date.
5. Stages all generated files with git add.

Outputs land in the same location that memory-bootstrap (the AI skill) would
produce: .agents/memory/daily/YYYY-MM-DD/events/ for shards and
.agents/memory/adr/ for promoted decisions.

Safety
------
A lock file (.agents/memory/.auto_bootstrap_running) prevents concurrent runs.
If the process exits uncleanly, the lock is left behind; session-start.py will
skip spawning a new run until the lock is manually removed or has been present
for longer than LOCK_TTL_SECONDS.

Requirements
------------
- ANTHROPIC_API_KEY environment variable must be set.
- Python 3.14+ (stdlib only -- no anthropic SDK needed).
- The repo must already be wired (bootstrap-repo.py has run).

Usage (called by session-start.py, not directly by the user)
-----
    auto-bootstrap.py --repo-root /path/to/repo [--max-commits 24]
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from collections import OrderedDict
from pathlib import Path

# Prepend script directory so common.py is importable when called as a
# detached subprocess (the parent hook's sys.path is not inherited).
sys.path.insert(0, str(Path(__file__).parent))
from common import (
    append_hook_trace,
    git,
    render_frontmatter,
    safe_main,
    utc_now,
    utc_timestamp,
    write_text,
)

_ANTHROPIC_API_URL = "https://api.anthropic.com/v1/messages"
_ANTHROPIC_MODEL = "claude-opus-4-6"
_ANTHROPIC_VERSION = "2023-06-01"

# Lock file lives inside the repo's memory directory.
_LOCK_FILENAME = ".auto_bootstrap_running"

# Remove stale locks older than this many seconds (crash recovery).
_LOCK_TTL_SECONDS = 300  # 5 minutes

_MAX_CONTEXT_CHARS = 80_000  # hard cap on total context sent to API

_SYSTEM_PROMPT = """\
You are a memory bootstrap agent for a shared repo memory system.

Your task: analyze the repository context (recent commits and design-like docs)
and identify the 3–7 most important durable architectural decisions.

Selection criteria:
- Favor: architecture boundaries, canonical data sources, API contracts,
  tool/dependency choices, output conventions, invariants, accepted tradeoffs.
- Reject: tasks, rollout sequencing, local optimizations, implementation trivia.
- Cluster related commits/docs into one decision family rather than one shard
  per paragraph or commit.

For each decision, use the SOURCE DATE (from the commit or doc, not today) for
the shard date fields.  Using today's date is wrong.

Return ONLY a valid JSON object — no markdown fences, no prose — matching this
exact schema:

{
  "shards": [
    {
      "source_date": "YYYY-MM-DD",
      "source_timestamp": "YYYY-MM-DDTHH:MM:SSZ",
      "title": "short imperative title",
      "decision_candidate": true,
      "files_touched": ["path/to/relevant/file"],
      "why": "one or two sentences explaining the motivation",
      "what_changed": ["bullet describing the decision or change"],
      "evidence": ["commit <hash>: <message>", "doc <path>: <section heading>"],
      "next": ["likely follow-up or implication"]
    }
  ]
}

Emit between 3 and 7 shards.  If the source material does not justify even one
durable decision, return {"shards": []}.
"""


# ---------------------------------------------------------------------------
# Lock helpers
# ---------------------------------------------------------------------------


def _lock_path(repo_root: Path) -> Path:
    return repo_root / ".agents" / "memory" / _LOCK_FILENAME


def _acquire_lock(repo_root: Path) -> bool:
    """Write the lock file.  Returns False if a fresh lock already exists."""
    lock = _lock_path(repo_root)
    if lock.exists():
        age = utc_now().timestamp() - lock.stat().st_mtime
        if age < _LOCK_TTL_SECONDS:
            return False  # another run is active
        lock.unlink(missing_ok=True)  # stale lock — remove and proceed
    lock.write_text(utc_timestamp(), encoding="utf-8")
    return True


def _release_lock(repo_root: Path) -> None:
    _lock_path(repo_root).unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Context collection
# ---------------------------------------------------------------------------


def _git_log(repo_root: Path, max_commits: int) -> str:
    """Return a compact git log for the last max_commits commits."""
    fmt = "%H %ad %an: %s"
    try:
        return git(
            ["log", f"-{max_commits}", f"--format={fmt}", "--date=short"],
            repo_root,
        )
    except Exception:
        return ""


def _find_design_docs(repo_root: Path) -> list[Path]:
    """Return design-like Markdown docs that are tracked by git."""
    patterns = [
        "*design*",
        "*spec*",
        "*arch*",
        "*adr*",
        "*decision*",
        "*overview*",
        "*README*",
        "*readme*",
    ]
    candidates: list[Path] = []
    for pattern in patterns:
        candidates.extend(repo_root.rglob(pattern))
    # Keep only tracked markdown files, deduplicated, outside .agents/
    seen: set[Path] = set()
    result: list[Path] = []
    for p in candidates:
        if p.suffix.lower() not in (".md", ".txt", ".rst"):
            continue
        if ".agents" in p.parts or ".git" in p.parts:
            continue
        rp = p.resolve()
        if rp in seen:
            continue
        seen.add(rp)
        # Verify git tracks this file
        try:
            out = git(
                ["ls-files", "--error-unmatch", str(p.relative_to(repo_root))],
                repo_root,
                check=False,
            )
            if out is not None:
                result.append(p)
        except Exception:
            pass
    return result[:8]  # cap at 8 docs


def _collect_context(repo_root: Path, max_commits: int) -> str:
    """Build the context string sent to the API."""
    parts: list[str] = []

    log = _git_log(repo_root, max_commits)
    if log:
        parts.append(f"## Recent git log (last {max_commits} commits)\n\n{log}")

    for doc_path in _find_design_docs(repo_root):
        try:
            content = doc_path.read_text(encoding="utf-8", errors="replace")
            rel = doc_path.relative_to(repo_root)
            parts.append(f"## Design doc: {rel}\n\n{content[:8000]}")
        except OSError:
            continue

    context = "\n\n---\n\n".join(parts)
    # Hard cap to avoid exceeding context limits
    if len(context) > _MAX_CONTEXT_CHARS:
        context = context[:_MAX_CONTEXT_CHARS] + "\n\n[... truncated ...]"
    return context


# ---------------------------------------------------------------------------
# API call
# ---------------------------------------------------------------------------


def _call_api(context: str, api_key: str) -> dict:
    """Call the Anthropic messages API and return parsed JSON response body."""
    body = json.dumps(
        {
            "model": _ANTHROPIC_MODEL,
            "max_tokens": 4096,
            "system": _SYSTEM_PROMPT,
            "messages": [
                {
                    "role": "user",
                    "content": (
                        "Please analyze this repository context and produce the memory "
                        "bootstrap JSON.\n\n" + context
                    ),
                }
            ],
        }
    ).encode("utf-8")

    req = urllib.request.Request(
        _ANTHROPIC_API_URL,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "x-api-key": api_key,
            "anthropic-version": _ANTHROPIC_VERSION,
        },
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _extract_shards_json(api_response: dict) -> list[dict]:
    """Pull the shards list out of the API response content block."""
    content = api_response.get("content", [])
    for block in content:
        if block.get("type") == "text":
            text = block["text"].strip()
            # Strip accidental markdown fences
            if text.startswith("```"):
                text = text.split("```", 2)[1]
                if text.startswith("json"):
                    text = text[4:]
                text = text.rsplit("```", 1)[0].strip()
            parsed = json.loads(text)
            return parsed.get("shards", [])
    return []


# ---------------------------------------------------------------------------
# Shard writing
# ---------------------------------------------------------------------------


def _shard_filename(source_timestamp: str, repo_root: Path) -> str:
    """Build the canonical shard filename from the source timestamp."""
    author = (
        git(["config", "user.name"], repo_root, check=False).replace(" ", "").lower()
    )
    if not author:
        author = "auto"
    # Replace colons in time with dashes for filesystem safety
    ts_safe = source_timestamp.replace(":", "-").rstrip("Z").replace("Z", "")
    # Ensure format: YYYY-MM-DDTHH-MM-SS
    return f"{ts_safe}Z--{author}--auto-bootstrap.md"


def _write_shard(shard: dict, repo_root: Path, bootstrapped_at: str) -> Path | None:
    """Write one event shard to disk. Returns the shard path or None on error."""
    source_date = shard.get("source_date", "")
    source_timestamp = shard.get("source_timestamp", "")
    if not source_date or not source_timestamp:
        return None

    events_dir = repo_root / ".agents" / "memory" / "daily" / source_date / "events"
    events_dir.mkdir(parents=True, exist_ok=True)

    filename = _shard_filename(source_timestamp, repo_root)
    shard_path = events_dir / filename

    # Build frontmatter
    branch = (
        git(["rev-parse", "--abbrev-ref", "HEAD"], repo_root, check=False) or "main"
    )
    meta: OrderedDict = OrderedDict(
        [
            ("timestamp", source_timestamp),
            ("author", git(["config", "user.name"], repo_root, check=False) or "auto"),
            ("branch", branch),
            ("thread_id", "auto-bootstrap"),
            ("turn_id", "auto-bootstrap"),
            ("decision_candidate", shard.get("decision_candidate", True)),
            ("bootstrapped_at", bootstrapped_at),
            ("ai_generated", True),
            ("ai_model", _ANTHROPIC_MODEL),
            ("ai_tool", "auto-bootstrap"),
            ("ai_surface", "session-start-hook"),
            ("ai_executor", "local-agent"),
            ("related_adrs", []),
            ("files_touched", shard.get("files_touched", [])),
        ]
    )

    frontmatter = render_frontmatter(meta)

    what_changed = "\n".join(f"- {item}" for item in shard.get("what_changed", []))
    evidence = "\n".join(f"- {item}" for item in shard.get("evidence", []))
    next_steps = "\n".join(f"- {item}" for item in shard.get("next", []))

    body = (
        f"{frontmatter}\n\n"
        f"## Why\n\n{shard.get('why', '')}\n\n"
        f"## What changed\n\n{what_changed}\n\n"
        f"## Evidence\n\n{evidence}\n\n"
        f"## Next\n\n{next_steps}\n"
    )

    write_text(shard_path, body)
    return shard_path


def _rebuild_summary(repo_root: Path, date: str) -> None:
    """Call rebuild-summary.py for the given date."""
    script = Path(__file__).parent / "rebuild-summary.py"
    subprocess.run(
        [sys.executable, str(script), "--repo-root", str(repo_root), "--date", date],
        check=False,
        capture_output=True,
    )


def _git_add(paths: list[Path], repo_root: Path) -> None:
    """Stage the given files."""
    if not paths:
        return
    git(["add", "--"] + [str(p) for p in paths], repo_root, check=False)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description="Background semantic bootstrap")
    parser.add_argument("--repo-root", required=True)
    parser.add_argument("--max-commits", type=int, default=24)
    args = parser.parse_args()

    repo_root = Path(args.repo_root).resolve()
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        append_hook_trace(
            "AutoBootstrap",
            "skipped",
            repo_root=repo_root,
            details={"reason": "no_api_key"},
        )
        return 1

    if not _acquire_lock(repo_root):
        append_hook_trace(
            "AutoBootstrap",
            "skipped",
            repo_root=repo_root,
            details={"reason": "lock_held"},
        )
        return 0

    append_hook_trace("AutoBootstrap", "started", repo_root=repo_root)

    try:
        context = _collect_context(repo_root, args.max_commits)
        if not context.strip():
            append_hook_trace(
                "AutoBootstrap",
                "skipped",
                repo_root=repo_root,
                details={"reason": "no_context"},
            )
            return 0

        api_response = _call_api(context, api_key)
        shards_data = _extract_shards_json(api_response)

        if not shards_data:
            append_hook_trace(
                "AutoBootstrap",
                "noop",
                repo_root=repo_root,
                details={"reason": "no_shards_returned"},
            )
            return 0

        bootstrapped_at = utc_timestamp()
        written: list[Path] = []
        dates: set[str] = set()

        for shard in shards_data:
            path = _write_shard(shard, repo_root, bootstrapped_at)
            if path:
                written.append(path)
                dates.add(shard["source_date"])

        for date in sorted(dates):
            _rebuild_summary(repo_root, date)
            summary_path = (
                repo_root / ".agents" / "memory" / "daily" / date / "summary.md"
            )
            if summary_path.exists():
                written.append(summary_path)

        _git_add(written, repo_root)

        append_hook_trace(
            "AutoBootstrap",
            "success",
            repo_root=repo_root,
            details={
                "shards_written": len([p for p in written if "events" in str(p)]),
                "dates": sorted(dates),
            },
        )
        return 0

    except Exception as exc:
        append_hook_trace(
            "AutoBootstrap", "error", repo_root=repo_root, details={"error": str(exc)}
        )
        return 1
    finally:
        _release_lock(repo_root)


if __name__ == "__main__":
    raise SystemExit(safe_main(main, "AutoBootstrap"))
