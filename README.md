# cheap-openclaw

14 production-tested techniques to cut your OpenClaw agent costs by 10x.

I run OpenClaw as my family's autonomous butler — [Spratt](https://github.com/kmanan/spratt-skills). He handles iMessage conversations, morning briefings, evening digests, health monitoring, email scanning, grocery tracking, flight alerts, and household automation, 24/7. The skills I built for that are in the [spratt-skills](https://github.com/kmanan/spratt-skills) repo.

Below is every cost and performance optimization that emerged from running Spratt in production. These aren't theories — they're battle-tested against a month of real traffic, real cron jobs, and many painful cost spikes. Every technique includes the config, the rationale, and what went wrong when we got it wrong.

---

## Table of Contents

1. [Intelligent Model Routing](#1-intelligent-model-routing)
2. [Cron Job Model Tiering](#2-cron-job-model-tiering)
3. [Exec Payloads for Deterministic Work](#3-exec-payloads-for-deterministic-work)
4. [Prompt Caching](#4-prompt-caching)
5. [lightContext for Cron Jobs](#5-lightcontext-for-cron-jobs)
6. [Bootstrap File Optimization](#6-bootstrap-file-optimization)
7. [Fallback Chain Optimization](#7-fallback-chain-optimization)
8. [Cron Session Cleanup](#8-cron-session-cleanup)
9. [Reply Noise Suppression](#9-reply-noise-suppression)
10. [Subagent Model Assignment](#10-subagent-model-assignment)
11. [Compaction and Context Pruning](#11-compaction-and-context-pruning)
12. [Usage Tracking](#12-usage-tracking)
13. [Prompt Compression](#13-prompt-compression)
14. [Session Reset on Idle](#14-session-reset-on-idle)

---

## Quick Start

If you want the biggest wins with the least effort, do these three first:

1. **Deploy a model router** for interactive sessions ([iblai-openclaw-router](https://github.com/iblai/iblai-openclaw-router)) — routes 80% of messages to Haiku instead of Sonnet. ~4x savings.
2. **Enable prompt caching** on Haiku (`cacheRetention: "short"`) — 90% discount on cached input tokens for your highest-volume model.
3. **Set `lightContext: true`** on all cron jobs that don't need full agent context — eliminates ~15-20K chars of bootstrap re-injection per cron turn.

Combined, these three changes alone can cut your bill by 5-8x.

---

## 1. Intelligent Model Routing

**The idea:** Not every message needs your most expensive model. A routing proxy scores each incoming message and sends it to the cheapest model that can handle it.

### Setup

Deploy [iblai-openclaw-router](https://github.com/iblai/iblai-openclaw-router) as a local proxy. It's a lightweight Node.js server (449 lines, zero external dependencies) that sits between OpenClaw and the Anthropic API.

Set it as your primary model in `openclaw.json`:

```json
{
  "agents": {
    "defaults": {
      "model": {
        "primary": "iblai-router/auto"
      }
    }
  }
}
```

### Three-Tier System

| Tier | Model | Cost (input/output per M tokens) |
|---|---|---|
| LIGHT | `claude-haiku-4-5-20251001` | $0.80 / $4.00 |
| MEDIUM | `claude-sonnet-4-6` | $3.00 / $15.00 |
| HEAVY | `claude-sonnet-4-6` (or Opus if you need it) | $3.00 / $15.00 |

### How Scoring Works

The router evaluates the **last user message only** (not the full conversation history) across 14 weighted dimensions:

| Dimension | Weight | Effect |
|---|---|---|
| `reasoningMarkers` | 0.18 | Complex reasoning keywords push toward MEDIUM/HEAVY |
| `codePresence` | 0.14 | Code-related terms push toward MEDIUM |
| `simpleIndicators` | 0.06 | Greetings, acknowledgments push toward LIGHT |
| `relayIndicators` | 0.05 | "Tell X...", "Send to..." push toward LIGHT |
| `tokenCount` | 0.08 | Short messages (-0.8), long messages (+0.8) |
| ... and 9 more | 0.49 | Technical terms, multi-step, creative, agentic, etc. |

The final score maps to a tier:
- Score < `lightMedium` boundary -> LIGHT (Haiku)
- Score < `mediumHeavy` boundary -> MEDIUM (Sonnet)
- Score >= `mediumHeavy` -> HEAVY

### Tuning Tips

The default boundaries are a good start, but you'll want to tune for your traffic:

- **Too much hitting Sonnet?** Lower the `lightMedium` boundary (e.g., from 0.08 to 0.05) to widen the LIGHT band.
- **Quality suffering on simple tasks?** Raise it back.
- **BlueBubbles/iMessage metadata inflating scores?** The router scores the raw message including channel metadata (~100-150 tokens per message). Adjust `tokenCount` thresholds upward to compensate.
- **Watch the confidence threshold.** When the score is near a boundary, the router computes a sigmoid confidence. Below the threshold, it defaults to MEDIUM as a safety net. Lower the threshold to let more borderline cases stay LIGHT.

### Hard Overrides

Two conditions bypass scoring entirely, forcing HEAVY:
1. 2+ reasoning keywords matched
2. Estimated tokens > 50,000

### What We Measured

After tuning: **80% LIGHT (Haiku), 20% MEDIUM (Sonnet)** across 564 routing decisions. That's a ~4x cost reduction vs. sending everything to Sonnet.

---

## 2. Cron Job Model Tiering

**The idea:** Match the model to what the job actually does. Content composition for humans needs quality (Haiku). Orchestration and checks just need to work (Flash).

### The Rule

| Task Type | Model | Why |
|---|---|---|
| Briefings, digests, summaries | Claude Haiku 4.5 | 90% data retrieval, 10% formatting. Haiku is great at structured formatting. |
| Health checks, scrapers, inspections | Gemini Flash | Orchestration work. Cheapest option at $0.30/$2.50 per M. |
| Interactive sessions | Router (see above) | Per-turn routing. |

### Example jobs.json

```json
{
  "jobs": [
    {
      "name": "Morning Briefing",
      "model": "anthropic/claude-haiku-4-5-20251001",
      "sessionTarget": "isolated",
      "payload": { "kind": "agentTurn", "message": "Run the morning briefing pipeline" },
      "lightContext": true
    },
    {
      "name": "Health Check",
      "model": "google/gemini-2.5-flash",
      "sessionTarget": "isolated",
      "payload": { "kind": "agentTurn", "message": "Run health checks" },
      "lightContext": true
    }
  ]
}
```

### Cost Difference

Switching briefings from Sonnet ($3/$15) to Haiku ($0.80/$4) is a **~12x cost reduction** on those jobs. Switching orchestration from Haiku to Flash ($0.30/$2.50) saves another ~3x.

### Pitfall: Don't Let Claude "Optimize" Your Models

If you have model assignment rules in your CLAUDE.md or AGENTS.md, be explicit. AI assistants love to "optimize" by upgrading cheap models to expensive ones or downgrading quality-sensitive jobs to the cheapest option. State the rule clearly and explain why.

---

## 3. Exec Payloads for Deterministic Work

**The idea:** If a cron job runs a shell script, don't spin up an LLM session to do it.

### Before (wasteful)

```
Cron job -> agentTurn -> LLM session -> LLM reads system prompt -> LLM calls tool -> script runs
```

You're paying for: system prompt injection, LLM reasoning about what to do, tool call overhead. For a shell command.

### After (free)

```
Cron job -> exec -> script runs directly
```

Zero tokens. Zero LLM involvement.

### Configuration

```json
{
  "name": "Nightly Backup",
  "payload": {
    "kind": "exec",
    "command": "/path/to/backup.sh"
  }
}
```

### Caveat

`sessionTarget: "isolated"` **requires** `agentTurn` — exec payloads are silently skipped for isolated sessions. If a job must be isolated and runs a script, wrap it in a minimal agentTurn:

```json
{
  "sessionTarget": "isolated",
  "payload": {
    "kind": "agentTurn",
    "message": "Run: /path/to/script.sh"
  }
}
```

This still costs tokens for the prompt, but far less than a full agent turn with tools.

---

## 4. Prompt Caching

**The idea:** Anthropic's prompt caching gives a 90% discount on repeated input content. Your system prompt (bootstrap files) is nearly identical every turn — cache it.

### Configuration

```json
{
  "agents": {
    "defaults": {
      "model": {
        "models": {
          "anthropic/claude-haiku-4-5-20251001": {
            "params": { "cacheRetention": "short" }
          },
          "anthropic/claude-sonnet-4-6": {
            "params": { "cacheRetention": "short" }
          }
        }
      }
    }
  }
}
```

### Why Haiku Matters Most

If you're using a router that sends 80% of traffic to Haiku, Haiku is your highest-volume model. Without caching, every Haiku turn re-processes the full system prompt (~15-20K chars). With caching, that content gets a 90% discount after the first turn.

See [`examples/openclaw.json`](examples/openclaw.json) for a complete annotated configuration.

---

## 5. lightContext for Cron Jobs

**The idea:** Cron jobs running pipelines or scripts don't need your agent's full personality, tool reference data, memory files, and behavioral rules injected into their session.

### Configuration

Add `"lightContext": true` to any cron job that doesn't need full agent context:

```json
{
  "name": "Morning Briefing",
  "lightContext": true,
  "payload": { "kind": "agentTurn", "message": "..." }
}
```

### What It Does

With `lightContext: true`, the cron session starts with minimal bootstrap content instead of injecting all your workspace files (AGENTS.md, SOUL.md, TOOLS.md, MEMORY.md, etc.). For a briefing job that just runs a pipeline, the model doesn't need personality rules or Home Assistant entity IDs.

### What to Enable It On

- Briefing/digest pipelines (they get their instructions from the pipeline, not bootstrap)
- Health checks and inspections
- Any job that executes a well-defined script or workflow

### What to Leave It Off

- Jobs that need to interpret unstructured data using agent knowledge
- Jobs that use tools requiring full context (e.g., email scanning with classification rules in AGENTS.md)

---

## 6. Bootstrap File Optimization

**The idea:** OpenClaw injects workspace bootstrap files into every turn's system prompt, capped at 20K chars per file and 150K total. Everything in those files costs tokens on every single turn.

### What to Do

1. **Audit your bootstrap files.** Check the sizes of AGENTS.md, SOUL.md, TOOLS.md, IDENTITY.md, USER.md, MEMORY.md. Anything close to the 20K limit is probably carrying dead weight.

2. **Move reference data to skills.** Device IDs, entity tables, API endpoint lists, routing tables — these belong in a skill's SKILL.md, loaded on-demand. OpenClaw injects only a compact manifest line per skill at bootstrap.

3. **Move domain-scoped rules to skills.** Instructions that only matter for specific tasks (briefing formatting, trip management, flight tracking) should be lazy-loaded skills, not always-injected rules.

4. **Set `bootstrapMaxChars` as a safety net:**

```json
{
  "agents": {
    "defaults": {
      "bootstrapMaxChars": 20000
    }
  }
}
```

### Skill-Based Lazy Loading

OpenClaw's skill architecture is the prescribed way to avoid bootstrap bloat:

- At bootstrap: only skill name + description + path are injected (one line per skill)
- On demand: full SKILL.md is loaded when the model decides it needs that skill
- Result: domain knowledge costs zero tokens when irrelevant

---

## 7. Fallback Chain Optimization

**The idea:** When your primary model hits rate limits (429s) or spending caps, the fallback chain determines what happens next. A bad fallback chain can cost you 10x.

### The Problem

Default fallback: `Flash -> Sonnet -> ...`

When Flash 429s (common on free tiers or spending caps), your health check suddenly runs on Sonnet at $3/$15 per M tokens. A heartbeat that runs every 30 minutes hits Sonnet 48 times/day.

### The Fix

```json
{
  "agents": {
    "defaults": {
      "model": {
        "fallbacks": [
          "anthropic/claude-haiku-4-5-20251001",
          "google/gemini-2.5-flash"
        ]
      }
    }
  }
}
```

**Flash always falls back to Haiku, never Sonnet.** Apply this both system-wide and on any per-agent overrides.

### Per-Agent Override

If an agent has its own model config, it needs its own fallback:

```json
{
  "agents": {
    "my-heartbeat": {
      "model": {
        "primary": "google/gemini-2.5-flash",
        "fallbacks": ["anthropic/claude-haiku-4-5-20251001"]
      }
    }
  }
}
```

---

## 8. Cron Session Cleanup

**The idea:** OpenClaw's lossless-claw context engine accumulates messages across isolated cron sessions, even though each run gets a new session ID. Left unchecked, this "session rot" wastes tokens and eventually breaks jobs.

### The Problem

`sessionTarget: "isolated"` creates a new `sessionId` each run, but lossless-claw indexes by `sessionKey`. Every run's messages accumulate under the same key. After ~25 days of daily runs, 100+ messages of dead context get replayed into each new session.

Symptoms:
- Haiku returns empty responses (overwhelmed by stale context)
- Token usage climbs steadily over days/weeks
- Flash survives longer but eventually rots too

### The Fix

Run [`scripts/cron-session-cleanup.py`](scripts/cron-session-cleanup.py) daily. It:

1. Reads `jobs.json` to find all enabled isolated cron jobs
2. Removes their session entries from `sessions.json`
3. Deactivates their conversations in `lcm.db` (the lossless-claw database)
4. Archives transcript files with 4-week retention

Schedule it via cron, launchd, or systemd:

```bash
# Run daily at 3:30 AM
0 3 30 * * * python3 /path/to/cron-session-cleanup.py
```

Or on macOS via launchd (see [`examples/com.openclaw.cron-session-cleanup.plist`](examples/com.openclaw.cron-session-cleanup.plist)).

> **Note:** This is a workaround for an upstream issue in OpenClaw/lossless-claw. If a future version adds native session rotation for isolated cron jobs, this script becomes unnecessary.

---

## 9. Reply Noise Suppression

**The idea:** Without configuration, OpenClaw streams intermediate text blocks as separate messages. A single task can produce 10+ messages of "Let me try X...", "That didn't work...", tool call narration. Each message is generated output tokens and gets re-injected as conversation history (input tokens).

### Configuration

```json
{
  "agents": {
    "defaults": {
      "verboseDefault": "off",
      "blockStreamingDefault": "off",
      "blockStreamingBreak": "message_end"
    }
  },
  "channels": {
    "bluebubbles": {
      "blockStreaming": false
    }
  }
}
```

### Prompt Engineering

Add a directive to your SOUL.md:

```markdown
## Working Silently
Never narrate your tool use or debugging process. Do not send messages like
"Let me check...", "I'll try...", "That didn't work, let me...". Complete
the task and send only the final result.
```

### What This Saves

- Fewer output tokens generated (no intermediate narration)
- Smaller conversation history (less re-injection on subsequent turns)
- Fewer messages delivered to users (better UX)

---

## 10. Subagent Model Assignment

**The idea:** Subagents handle focused subtasks (data lookups, formatting, parallel queries). Their outputs are consumed by the parent agent, not humans. They don't need your most expensive model.

### Configuration

```json
{
  "agents": {
    "defaults": {
      "subagents": {
        "model": "google/gemini-2.5-flash",
        "maxConcurrent": 8
      }
    }
  }
}
```

Flash at $0.30/$2.50 vs. inheriting the parent's Sonnet at $3.00/$15.00 — that's 10x cheaper per subagent call.

---

## 11. Compaction and Context Pruning

**The idea:** Conversations grow. Each turn re-sends the entire history. Without management, the 10th message re-transmits all 9 previous turns, and token costs grow quadratically.

### Context Pruning

```json
{
  "agents": {
    "defaults": {
      "contextPruning": {
        "mode": "cache-ttl",
        "ttl": "15m"
      }
    }
  }
}
```

After 15 minutes idle, stale context is pruned. Adjust the TTL based on how long your typical conversations last.

### Compaction

```json
{
  "agents": {
    "defaults": {
      "compaction": {
        "mode": "safeguard",
        "reserveTokensFloor": 40000
      }
    }
  }
}
```

When approaching context limits, compaction summarizes older turns. Use Haiku for the summary model:

```json
{
  "plugins": {
    "lossless-claw": {
      "summaryModel": "anthropic/claude-haiku-4-5-20251001"
    }
  }
}
```

### Memory Flush

```json
{
  "compaction": {
    "memoryFlush": {
      "enabled": true,
      "softThresholdTokens": 4000
    }
  }
}
```

When in-context memory exceeds 4K tokens, flush it to disk rather than carrying it in the conversation window.

---

## 12. Usage Tracking

**The idea:** You can't optimize what you can't measure. Track routing decisions and estimate costs daily to catch regressions early.

### Router Logging

The iblai-router logs every routing decision to `routing.csv`:

```
timestamp,tier,model,score,confidence,reasoning,tokens,query
2026-04-06T14:30:00Z,LIGHT,claude-haiku-4-5-20251001,0.02,0.89,scored,450,
2026-04-06T14:31:00Z,MEDIUM,claude-sonnet-4-6,0.12,0.72,scored,1200,"explain the trip..."
```

### Daily Usage Script

[`scripts/daily-usage.py`](scripts/daily-usage.py) parses `routing.csv` and appends daily summaries to `usage-history.csv`:

```bash
python3 scripts/daily-usage.py           # Today
python3 scripts/daily-usage.py 2026-04-05  # Specific date
```

Output:
```
Usage for 2026-04-06:
  Total: 245 requests, ~$0.5894
  LIGHT: 93 requests, ~4200 tokens, ~$0.0032
  MEDIUM: 152 requests, ~82000 tokens, ~$0.5862
```

### What to Watch For

- LIGHT percentage dropping below 70% — your router boundaries may need retuning
- A spike in MEDIUM requests — check if a new tool or prompt is inflating scores
- Sudden cost jump — check the fallback chain, a model may be 429ing

---

## 13. Prompt Compression

**The idea:** Fewer tokens in, fewer tokens charged. Write terse prompts and pre-structure data.

### Extraction Prompts

Before (45 lines):
```
Please extract the following information from the text below.
The output should be a valid JSON object with these fields:
- "title": The title of the event (string or null if not found)
- "date": The date in ISO format (string or null)
...
[20 more lines of explanation]
[Pretty-printed JSON example]
```

After (3 lines):
```
Extract from text as JSON: {"title":str|null,"date":str|null,"location":str|null}
Only include fields present. Return valid JSON, nothing else.
```

Same accuracy. ~15x fewer input tokens.

### Pipeline Architecture

Separate data gathering (deterministic scripts, zero tokens) from composition (single LLM call with pre-structured data). Don't make the LLM do what `grep`, `sqlite3`, or `jq` can do for free.

---

## 14. Session Reset on Idle

**The idea:** Long-running sessions accumulate context that inflates every subsequent turn. Reset them.

```json
{
  "session": {
    "reset": {
      "idleMinutes": 60
    }
  }
}
```

After 60 minutes of inactivity, the session resets. The next interaction starts fresh instead of carrying stale history. Adjust based on your usage patterns — shorter for chatbot-style agents, longer for agents doing extended work.

---

## Summary

| Technique | What It Does | Savings |
|---|---|---|
| Model routing | 80% of interactive traffic to Haiku | ~4x |
| Cron model tiering | Haiku for composition, Flash for orchestration | ~10-15x on cron |
| Exec payloads | Skip LLM for deterministic work | 100% on orchestration |
| Prompt caching | 90% discount on repeated system prompt | ~90% on cached input |
| lightContext | Minimal bootstrap for cron jobs | ~80% per cron turn |
| Bootstrap optimization | Move reference data to lazy-loaded skills | ~15-20% per turn |
| Fallback chain | Flash -> Haiku (not Sonnet) on 429s | Prevents 10x spike events |
| Session cleanup | Daily cron session rot prevention | Prevents unbounded growth |
| Reply suppression | No intermediate messages | Reduces output + history |
| Subagent assignment | Flash for all subagents | ~10x vs. Sonnet |
| Compaction | Haiku-based summarization | Cheap context management |
| Usage tracking | CSV logging + daily reports | Catches regressions |
| Prompt compression | Terse prompts, pre-structured data | Per-prompt savings |
| Session reset | Idle session reset | Prevents stale carry |

---

## Scripts

- [`scripts/cron-session-cleanup.py`](scripts/cron-session-cleanup.py) — Daily cleanup of isolated cron session rot
- [`scripts/daily-usage.py`](scripts/daily-usage.py) — Parse router logs and track daily costs

## Examples

- [`examples/openclaw.json`](examples/openclaw.json) — Annotated config with all cost-relevant settings
- [`examples/jobs.json`](examples/jobs.json) — Example cron job configurations with model tiering
- [`examples/com.openclaw.cron-session-cleanup.plist`](examples/com.openclaw.cron-session-cleanup.plist) — macOS launchd schedule for session cleanup

---

## License

MIT

---

*Built by [@kmanan](https://github.com/kmanan) running [Spratt](https://github.com/kmanan/spratt-skills), a 24/7 autonomous OpenClaw family butler. These optimizations emerged from a month of production operation and many painful cost spikes.*
