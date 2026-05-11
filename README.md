# Collaborative Shared Repo Memory

A collaborative shared repo memory system for fast-moving software work. It helps people, agents, and teams stay up-to-date and aligned across a fast-paced change landscape by capturing why decisions were made, what changed, and what comes next.

Current version: `0.4.4`

---

## What It Does

Coding agents are productive inside a single session and fragile across time. Teams are productive within one meeting or one PR and then lose context as the change landscape moves. This system gives people and agents durable shared repo context so each new session starts from prior decisions instead of rebuilding history from scratch.

**Memory is plain Markdown committed to Git.** There is no external service, no vector database, and no embedding pipeline. The repo owns the memory, Git moves it, and people, agents, and teams can all stay aligned from the same source of truth.

### Key concepts

| Concept | What it is |
| ------- | ---------- |
| **Turn** | One prompt-response interaction or hook event. Turns remain provenance and traceability metadata; they are not the durable memory unit. |
| **File-changing turn** | A turn that changed at least one repo file in the working tree, including newly created files. Only file-changing turns may produce pending captures; conversational turns with no repo changes are silently skipped. |
| **Pending capture** | Local-only mechanical capture for one file-changing turn. Lives under `.agents/memory/pending/YYYY-MM-DD/`, contains no raw prompt or raw assistant text, and must never be committed. |
| **Episode cluster** | A bounded, semantically related collection of pending captures derived by the local episode graph. It is the semantic unit evaluated for publication, not a durable artifact. |
| **Checkpoint** | Durable published memory synthesized from an episode cluster plus repo-grounded context. Lives under `.agents/memory/daily/YYYY-MM-DD/events/`. |
| **Daily summary** | Derived read model rebuilt deterministically from that day's published checkpoints. Never edited directly. |
| **ADR** | Architecture Decision Record. Promoted explicitly from decision-candidate checkpoints. The only location for durable repo decisions. |
| **Local catch-up** | Uncommitted digest rebuilt after `git pull`, checkout, or merge. Tells the current agent what changed since it last ran. |

### Storage layout

```
<repo>/
├── .agents/memory/          # shared memory root (published + local-only staging)
│   ├── adr/                 # architecture decision records
│   │   └── INDEX.md
│   ├── daily/
│   │   └── YYYY-MM-DD/
│   │       ├── events/      # immutable published checkpoints
│   │       └── summary.md   # derived daily summary
│   ├── pending/             # local-only pending captures (gitignored)
│   ├── state/               # local-only derived episode-graph state (gitignored)
│   │   ├── checkpoint-context/
│   │   └── episode-graph/
│   │       └── episodes/
│   └── logs/                # local-only diagnostic logs (gitignored)
├── .githooks/               # generated repo-local hooks (gitignored)
│   └── pre-commit           # blocks pending/raw shards, then runs optional project checks
└── .codex/
    └── memory -> ../.agents/memory   # Codex access path (symlink)

~/.agent/shared-repo-memory/   # installed helper scripts
~/.agent/state/                # refresh state
```

---

## How Memory Flows

```
SessionStart hook
    → validates installed assets
    → bootstraps repo wiring if needed
    → injects ADR index + recent summaries into agent context

Agent turn completes
    → Stop / AfterAgent hook fires post-turn-notify.py
    → file-changing turn? → one privacy-safe pending capture written under .agents/memory/pending/
    → bounded local episode cluster derived from pending captures
    → local episode manifest written under .agents/memory/state/episode-graph/episodes/
    → background memory-checkpointer subagent evaluates the episode cluster
    → only if the candidate passes validation: published checkpoint written under daily/events/
    → summary rebuilt from the published checkpoint set
    → published checkpoint + summary auto-staged

Developer commits + pushes (same PR as the code)
    → shared memory becomes collaborative

git pull / checkout / merge
    → Git hooks rebuild local catch-up
    → next session resumes from bounded local digest
```

Shared memory is not collaborative until explicitly committed and pushed. The system never auto-commits or auto-pushes.

---

## Prerequisites

- Python 3.14+
- Git
- One or more supported agents: **Claude Code** (primary), **Gemini CLI**, or Codex CLI

---

## Installation

Clone this repo, then run the installer from the repo root:

```bash
git clone <this-repo-url>
cd agentmemory
./install.sh
```

The installer:

1. Copies helper scripts to `~/.agent/shared-repo-memory/`
2. Wires the supported hooks for each agent and reports the current support limits (see Agent Support below)
3. Sets `shared_repo_memory_configured = true` in agent config files
4. Initializes refresh state under `~/.agent/state/`
5. Copies memory skills into `~/.agent/skills/` and symlinks each into `~/.claude/skills/`, `~/.codex/skills/`, and `~/.gemini/skills/`

### Options

```bash
./install.sh --dry-run    # preview every action without making changes
./install.sh --force      # replace conflicting installed skill copies
```

### After installation

Restart any open agent sessions. `SessionStart` validates and bootstraps repo-local wiring on the next session open — it creates `.agents/memory/`, `.agents/memory/pending/`, `.codex/memory`, `.codex/local/`, and `.githooks/` if any are missing, repairs the agentmemory-managed `.gitignore` block when local-only paths change, and restores the generated repo-local hook files when hook wiring drifts. Generated hook files include a provenance header so developers can see that agentmemory created them, and the generated `pre-commit` hook delegates to `scripts/shared-repo-memory/project-pre-commit.sh` when a repo wants tracked project-specific checks such as linting or tests.

### Uninstalling

Two scopes, symmetric with install, run from the repo root. `./install.sh --uninstall` is an alias for `./uninstall.sh` that dispatches with the remaining flags forwarded verbatim, so `--dry-run`, `--repo`, and `--purge-memory` all behave identically under either entry point.

```bash
./uninstall.sh                          # global: remove ~/.agent/shared-repo-memory,
                                        # skill symlinks, and agent hook wiring
./uninstall.sh --repo                   # per-repo: remove .githooks/, unset
                                        # core.hooksPath, drop .codex/memory symlink,
                                        # strip the installer's .gitignore block
./uninstall.sh --repo --purge-memory    # also stage git rm --cached .agents/memory
                                        # (review and commit manually)
./uninstall.sh --dry-run                # preview every action without making changes
./install.sh --uninstall [flags]        # alias: dispatches to uninstall.py with flags
```

Uninstall is idempotent and conservative: it removes only entries it can identify as its own (commands pointing at `~/.agent/shared-repo-memory/`, hook names starting with `shared-repo-memory-`, canonical git hook content produced by `bootstrap-repo.py`, and the specific `.gitignore` marker block). Any user-added hook, edited git hook, or custom config value is preserved. Committed memory artifacts under `.agents/memory/` are never touched without explicit `--purge-memory`.

After a global uninstall, restart any open agent sessions so the removed hook wiring takes effect.

---

## Agent Support

| Hook | Purpose | Claude Code | Gemini CLI | Codex |
| ---- | ------- | ----------- | ---------- | ----- |
| Session start | Validate wiring, inject memory context | `SessionStart` | `SessionStart` | `SessionStart` |
| Post-turn capture | Write pending capture and spawn checkpoint flow | `Stop` | `AfterAgent` | Not provisioned |
| Subagent capture | Write pending capture for Task agent turns | `SubagentStop` | — | — |
| Pre-turn guard | Detect empty memory, offer bootstrap | `UserPromptSubmit` | `BeforeAgent` | `UserPromptSubmit` |
| Post-compaction | Re-inject memory after context compaction | `PostCompact` | — | — |

Codex support is intentionally explicit: today the supported surface is `SessionStart` plus the pre-turn bootstrap guard. The repo keeps `notify-wrapper.sh` as a manual smoke-test path for `post-turn-notify.py`, but the installer does not claim native Codex post-turn parity.

All hook scripts (`session-start.py`, `prompt-guard.py`, `post-compact.py`) emit a unified JSON schema accepted by all agents. `post-turn-notify.py` detects the calling agent from `hookEventName` to set AI attribution fields when the runtime exposes a supported post-turn event.

---

## What Happens at Session Start

When you open Claude Code in a wired repo, the `SessionStart` hook fires automatically and:

1. Validates that installed assets, refresh state, and repo wiring are all reachable
2. Bootstraps any missing repo-local wiring
3. Shows a notification in the UI: _"agentmemory loaded. Last refresh: …"_
4. Injects the ADR index and recent daily summaries into the model's context
5. If no event shards exist yet, spawns a current-runtime background bootstrap only when that runtime exposes a supported non-interactive bootstrap command; otherwise it leaves bootstrap manual via `/memory-bootstrap`

You do not need to ask the agent to read memory — it arrives as session context.

---

## Configuration

The installer writes all config. These are the relevant keys per agent.

**`~/.claude/settings.json`** — Claude Code:

```json
{
  "shared_repo_memory_configured": true,
  "shared_agent_assets_repo_path": "/path/to/this/repo",
  "hooks": {
    "SessionStart": [
      { "hooks": [{ "type": "command", "command": "~/.agent/shared-repo-memory/session-start.py", "timeout": 30 }] }
    ],
    "Stop": [
      { "hooks": [{ "type": "command", "command": "~/.agent/shared-repo-memory/post-turn-notify.py", "timeout": 60 }] }
    ]
  }
}
```

**`~/.codex/config.toml`** — Codex:

```toml
experimental_use_hooks = true
hooks_config_path = "~/.codex/hooks.json"
shared_repo_memory_configured = true
shared_agent_assets_repo_path = "/path/to/this/repo"
```

Codex is wired for `SessionStart` and `UserPromptSubmit`. This repo does not currently provision a native Codex post-turn hook path.

**`~/.gemini/settings.json`** — Gemini CLI:

```json
{
  "shared_repo_memory_configured": true,
  "hooks": {
    "SessionStart": [
      {
        "matcher": "*",
        "hooks": [
          {
            "name": "shared-repo-memory-session-start",
            "type": "command",
            "command": "~/.agent/shared-repo-memory/session-start.py",
            "timeout": 30000
          }
        ]
      }
    ],
    "AfterAgent": [
      {
        "matcher": "*",
        "hooks": [
          {
            "name": "shared-repo-memory-post-turn",
            "type": "command",
            "command": "~/.agent/shared-repo-memory/post-turn-notify.py",
            "timeout": 30000
          }
        ]
      }
    ]
  }
}
```

To disable: set `shared_repo_memory_configured` to `false` or remove the hooks. `SessionStart` exits silently if the flag is absent or false.

---

## Skills

Four skills ship with this system.

### Why the symlink model exists

Each agent looks for skills in its own directory — Claude Code reads `~/.claude/skills/`, Codex reads `~/.codex/skills/`, Gemini reads `~/.gemini/skills/`. Without a shared layer, you'd have to maintain four separate copies and keep them in sync every time a skill changes.

The installer solves this with one canonical copy and per-agent symlinks:

```
~/.agent/skills/memory-writer/     ← one real copy, installed from this repo
    ↑
~/.claude/skills/memory-writer     symlink
~/.codex/skills/memory-writer      symlink
~/.gemini/skills/memory-writer     symlink
```

The real copy lives under `~/.agent/skills/` — a neutral location not owned by any single agent. Each agent's skill directory holds only a symlink into that copy. When a skill is updated, only the copy in `~/.agent/skills/` changes; all agents pick up the update automatically through their symlinks without any per-agent reinstall.

The skills are copied from this repo rather than symlinked directly to it. This keeps agents from crawling the entire repo as context when they load a skill.

### Skills reference

| Skill | Invoke when |
| ----- | ----------- |
| `memory-writer` | After a file-changing turn from a manual runtime path such as Codex notify-wrapper testing — delegates to `post-turn-notify.py` so the turn becomes a pending capture instead of a directly published shard |
| `memory-checkpointer` | A background episode cluster should be evaluated for durable publication without blocking the user turn |
| `memory-bootstrap` | First time in a repo with existing history — mines design docs and commits to seed initial decision candidates and promote foundational ADRs |
| `adr-promoter` | A decision-candidate shard should become a permanent ADR |
| `news` | "What's new?" / "Catch me up" — summarizes recent summaries and ADRs; invokes `memory-bootstrap` if the repo is wired but has no history yet |

---

## Normal Workflow

```
1. Open agent session in repo
   └── SessionStart injects ADR index + recent summaries into context

2. Do work with the agent

3. Agent turn ends
   └── Stop/AfterAgent hook runs post-turn-notify.py
   └── File-changing turn? → pending capture written
   └── Background checkpoint evaluation runs from the active local episode cluster
   └── Validation succeeds? → published checkpoint + day summary auto-staged

4. Review staged memory files alongside code changes

5. Commit and push (same PR as the code)
   └── Memory becomes collaborative

6. Teammates git pull
   └── Post-merge/post-checkout hooks rebuild local catch-up
   └── Next session picks up catch-up context automatically
```

---

## New Repo with Existing History

```
1. Run ./install.sh

2. Restart agent session
   └── SessionStart bootstraps repo wiring
   └── No event shards found → bootstrap subagent spawned automatically in background
   └── Shards appear in .agents/memory/daily/ within ~30 seconds

3. Review and commit the bootstrapped memory
```

To trigger bootstrap manually (e.g. after deleting shards): `/memory-bootstrap`

---

## ADR Promotion

Decision-candidate event shards are published checkpoints. ADRs are curated, durable decisions. Promotion is always explicit — it never happens automatically as a post-turn side effect.

To promote a candidate:

```
/adr-promoter
```

The skill reads decision-candidate shards, creates or updates the ADR file under `.agents/memory/adr/`, and rebuilds the ADR index. Commit the result alongside any related code.

---

## Validating the Install

### Check installed scripts and skills

```bash
ls ~/.agent/shared-repo-memory/
ls ~/.agent/skills/
ls ~/.claude/skills/
```

### Check repo wiring

```bash
ls -la .agents/memory/
ls -la .agents/memory/pending/
ls -la .codex/memory          # should be a symlink → ../.agents/memory
ls -la .githooks/
git config core.hooksPath     # should print .githooks
```

### Validate the post-turn write path

```bash
./scripts/shared-repo-memory/validate-notify.sh
```

Writes one synthetic pending capture through the manual `notify-wrapper.sh` path. This confirms the wrapper and `post-turn-notify.py` work together when invoked directly; durable publication still requires checkpoint validation, and the check does not prove native Codex post-turn hook support.

### Check the hook trace log

```bash
tail ~/.agent/state/shared-repo-memory-hook-trace.jsonl
```

Every hook invocation appends a JSONL entry. If `SessionStart` fired successfully you will see `"status": "success"` entries.

Shared-memory stderr logs and helper-script logs now include product and runtime
metadata in their prefix, for example `[agentmemory][version=0.4.4][runtime=codex][runtime-version=0.118.0]`.
Non-agent helper flows use explicit runtime ids too, for example `runtime=git-hook`
for post-checkout catch-up or `runtime=installer` during installation, rather
than falling back to `unknown`.

---

## Troubleshooting

| Problem                                                       | Fix                                                                                                      |
| ------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------- |
| Session starts but no memory context appears                  | Check the hook trace — if status is `success`, the hook ran. Restart the session.                        |
| Hook trace shows `skipped` with `shared_repo_memory_disabled` | `shared_repo_memory_configured` is not `true`. Re-run `./install.sh`.                                    |
| Hook trace shows `error` with `missing_required_paths`        | Re-run `./install.sh` to reinstall helper scripts and refresh state.                                     |
| `.codex/memory` is a real directory, not a symlink            | Delete it and re-run `./install.sh`.                                                                     |
| No pending capture written after a turn                       | The turn was not file-changing: no repo files changed in the working tree. Check `git status` — only turns that modify or create repo files produce pending captures. |
| `Permission denied` on `install.sh`                           | `chmod +x install.sh && ./install.sh`                                                                    |
