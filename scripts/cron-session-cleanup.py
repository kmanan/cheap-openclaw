#!/usr/bin/env python3
"""
cron-session-cleanup.py — Prevents isolated cron sessions from rotting.

Problem: OpenClaw's lossless-claw context engine indexes by session KEY,
not session ID. Isolated cron sessions get new IDs each run, but all
historical messages accumulate under the same key. After ~25 days of daily
runs, 100+ messages of dead context get replayed into each new session.
Haiku chokes and returns empty responses. Flash survives longer but rots too.

Fix: This script reads jobs.json, finds all enabled isolated jobs, and for
each one removes its session entries from sessions.json, deactivates their
conversations in lcm.db, and archives transcript files. The next cron run
starts completely fresh.

Usage:
  python3 cron-session-cleanup.py
  python3 cron-session-cleanup.py --agent my-agent
  python3 cron-session-cleanup.py --openclaw-dir /path/to/.openclaw
  python3 cron-session-cleanup.py --dry-run

Schedule daily via cron, launchd, or systemd. See examples/ for a launchd plist.
"""

import argparse
import json
import os
import shutil
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path


def log(msg: str, log_file: Path | None = None):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line)
    if log_file:
        try:
            log_file.parent.mkdir(parents=True, exist_ok=True)
            with open(log_file, "a") as f:
                f.write(line + "\n")
        except Exception:
            pass


def load_json(path: Path) -> dict:
    with open(path) as f:
        return json.load(f)


def save_json(path: Path, data: dict):
    tmp = path.with_suffix(".json.tmp")
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    tmp.rename(path)


def get_isolated_job_ids(jobs_file: Path) -> list[tuple[str, str]]:
    """Return (job_id, job_name) for all enabled isolated cron jobs."""
    jobs_data = load_json(jobs_file)
    results = []
    for job in jobs_data.get("jobs", []):
        if job.get("enabled") and job.get("sessionTarget") == "isolated":
            results.append((job["id"], job.get("name", job["id"])))
    return results


def cleanup_sessions(
    openclaw_dir: Path,
    agent_name: str = "spratt",
    dry_run: bool = False,
    log_file: Path | None = None,
):
    jobs_file = openclaw_dir / "cron" / "jobs.json"
    agent_sessions = openclaw_dir / "agents" / agent_name / "sessions"
    sessions_json = agent_sessions / "sessions.json"
    archive_dir = agent_sessions / "archived-cron-sessions"
    lcm_db = openclaw_dir / "lcm.db"

    if not jobs_file.exists():
        log(f"jobs.json not found at {jobs_file}", log_file)
        return

    if not sessions_json.exists():
        log("sessions.json not found, nothing to clean", log_file)
        return

    jobs = get_isolated_job_ids(jobs_file)
    if not jobs:
        log("No enabled isolated cron jobs found", log_file)
        return

    log(f"Found {len(jobs)} enabled isolated cron jobs", log_file)

    if dry_run:
        for job_id, job_name in jobs:
            log(f"  [DRY RUN] Would clean: {job_name} ({job_id})", log_file)
        return

    # Back up sessions.json before modifying
    backup_path = sessions_json.with_suffix(
        f".json.bak-cleanup-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
    )
    shutil.copy2(sessions_json, backup_path)
    log(f"Backed up sessions.json to {backup_path.name}", log_file)

    sessions = load_json(sessions_json)
    archive_ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    archive_subdir = archive_dir / archive_ts

    removed_keys = 0
    archived_files = 0

    for job_id, job_name in jobs:
        key_prefix = f"agent:{agent_name}:cron:{job_id}"

        # Find all session keys for this job
        keys_to_remove = [k for k in sessions if k.startswith(key_prefix)]
        if not keys_to_remove:
            log(f"  {job_name}: no session entries found, skipping", log_file)
            continue

        # Collect sessionIds to archive their transcript files
        session_ids = set()
        for key in keys_to_remove:
            entry = sessions[key]
            sid = entry.get("sessionId")
            if sid:
                session_ids.add(sid)

        # Remove entries from sessions.json
        for key in keys_to_remove:
            del sessions[key]
            removed_keys += 1

        # Archive transcript files
        for sid in session_ids:
            files = list(agent_sessions.glob(f"{sid}.*"))
            if files:
                archive_subdir.mkdir(parents=True, exist_ok=True)
                for f in files:
                    dest = archive_subdir / f.name
                    shutil.move(str(f), str(dest))
                    archived_files += 1

        log(
            f"  {job_name}: removed {len(keys_to_remove)} keys, archived {len(session_ids)} sessions",
            log_file,
        )

    # Save cleaned sessions.json
    save_json(sessions_json, sessions)

    log(f"Done: removed {removed_keys} session keys, archived {archived_files} files", log_file)

    # Clean up lossless-claw conversations
    if lcm_db.exists():
        try:
            conn = sqlite3.connect(str(lcm_db), timeout=10)
            deactivated = 0
            for job_id, job_name in jobs:
                key_prefix = f"agent:{agent_name}:cron:{job_id}"
                cur = conn.execute(
                    "UPDATE conversations SET active = 0, archived_at = datetime('now') "
                    "WHERE session_key LIKE ? AND active = 1",
                    (key_prefix + "%",),
                )
                if cur.rowcount > 0:
                    deactivated += cur.rowcount
                    log(
                        f"  {job_name}: deactivated {cur.rowcount} lcm.db conversation(s)",
                        log_file,
                    )
            conn.commit()
            conn.close()
            if deactivated:
                log(f"Total lcm.db conversations deactivated: {deactivated}", log_file)
            else:
                log("No active lcm.db cron conversations found", log_file)
        except Exception as e:
            log(f"WARNING: lcm.db cleanup failed: {e}", log_file)
    else:
        log("lcm.db not found, skipping context engine cleanup", log_file)

    # Clean up old archives (keep last 4 weeks)
    if archive_dir.exists():
        archives = sorted(archive_dir.iterdir())
        if len(archives) > 28:  # ~4 weeks of daily runs
            for old in archives[:-28]:
                if old.is_dir():
                    shutil.rmtree(old)
                    log(f"  Pruned old archive: {old.name}", log_file)


def main():
    parser = argparse.ArgumentParser(
        description="Clean up isolated cron session rot in OpenClaw"
    )
    parser.add_argument(
        "--openclaw-dir",
        type=Path,
        default=Path.home() / ".openclaw",
        help="Path to .openclaw directory (default: ~/.openclaw)",
    )
    parser.add_argument(
        "--agent",
        default="spratt",
        help="Agent name (default: spratt)",
    )
    parser.add_argument(
        "--log-file",
        type=Path,
        default=None,
        help="Path to log file (default: stdout only)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be cleaned without making changes",
    )
    args = parser.parse_args()

    log("=== Cron session cleanup starting ===", args.log_file)
    try:
        cleanup_sessions(
            openclaw_dir=args.openclaw_dir,
            agent_name=args.agent,
            dry_run=args.dry_run,
            log_file=args.log_file,
        )
    except Exception as e:
        log(f"ERROR: {e}", args.log_file)
        sys.exit(1)
    log("=== Cron session cleanup complete ===", args.log_file)


if __name__ == "__main__":
    main()
