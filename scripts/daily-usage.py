#!/usr/bin/env python3
"""
daily-usage.py — Parse router routing.csv logs and track daily API costs.

Reads the iblai-openclaw-router's routing.csv, aggregates requests by tier
and model for a given date, estimates costs, and appends a summary row to
usage-history.csv. Idempotent per day.

Usage:
  python3 daily-usage.py                          # Today's usage
  python3 daily-usage.py 2026-04-05               # Specific date
  python3 daily-usage.py --routing-csv /path/to/routing.csv
  python3 daily-usage.py --history-csv /path/to/usage-history.csv
"""

import argparse
import csv
import os
import sys
from datetime import datetime
from collections import defaultdict

# Pricing per million tokens (input/output) — update as prices change
DEFAULT_PRICING = {
    "claude-haiku-4-5-20251001": {"input": 0.80, "output": 4.00},
    "claude-sonnet-4-6": {"input": 3.00, "output": 15.00},
    "google/gemini-2.5-flash": {"input": 0.30, "output": 2.50},
}


def parse_date(ts_str):
    """Extract date from ISO timestamp."""
    try:
        return ts_str[:10]
    except Exception:
        return None


def already_logged(history_csv, date_str):
    """Check if this date already has a row in the history CSV."""
    if not os.path.exists(history_csv):
        return False
    with open(history_csv) as f:
        reader = csv.DictReader(f)
        for row in reader:
            if row.get("date") == date_str:
                return True
    return False


def parse_routing_csv(routing_csv, target_date):
    """Parse routing.csv and return stats for the target date."""
    stats = defaultdict(lambda: {"requests": 0, "est_tokens": 0})

    if not os.path.exists(routing_csv):
        return stats

    with open(routing_csv) as f:
        reader = csv.DictReader(f)
        for row in reader:
            row_date = parse_date(row.get("timestamp", ""))
            if row_date != target_date:
                continue

            tier = row.get("tier", "UNKNOWN")
            model = row.get("model", "unknown")
            tokens = int(row.get("tokens", 0) or 0)

            key = f"{tier}:{model}"
            stats[key]["requests"] += 1
            stats[key]["est_tokens"] += tokens

    return stats


def estimate_cost(model, est_input_tokens, pricing):
    """Rough cost estimate. Router only logs estimated input tokens.
    Assume output ~ 2x input tokens as heuristic."""
    p = pricing.get(model, {"input": 3.0, "output": 15.0})
    input_cost = (est_input_tokens / 1_000_000) * p["input"]
    output_est = est_input_tokens * 2
    output_cost = (output_est / 1_000_000) * p["output"]
    return round(input_cost + output_cost, 4)


def run(routing_csv, history_csv, target_date=None, pricing=None):
    if pricing is None:
        pricing = DEFAULT_PRICING
    if target_date is None:
        target_date = datetime.now().strftime("%Y-%m-%d")

    if already_logged(history_csv, target_date):
        print(f"Already logged for {target_date}, skipping.")
        return

    stats = parse_routing_csv(routing_csv, target_date)

    if not stats:
        print(f"No routing data for {target_date}.")
        return

    # Aggregate
    total_requests = 0
    total_cost = 0.0
    tier_summary = defaultdict(lambda: {"requests": 0, "est_tokens": 0, "est_cost": 0.0})

    for key, data in stats.items():
        tier, model = key.split(":", 1)
        cost = estimate_cost(model, data["est_tokens"], pricing)
        tier_summary[tier]["requests"] += data["requests"]
        tier_summary[tier]["est_tokens"] += data["est_tokens"]
        tier_summary[tier]["est_cost"] += cost
        total_requests += data["requests"]
        total_cost += cost

    # Write to history CSV
    init = not os.path.exists(history_csv)
    with open(history_csv, "a", newline="") as f:
        writer = csv.writer(f)
        if init:
            writer.writerow([
                "date",
                "total_requests", "total_est_cost",
                "light_requests", "light_est_tokens", "light_est_cost",
                "medium_requests", "medium_est_tokens", "medium_est_cost",
                "heavy_requests", "heavy_est_tokens", "heavy_est_cost",
            ])

        light = tier_summary.get("LIGHT", {})
        medium = tier_summary.get("MEDIUM", {})
        heavy = tier_summary.get("HEAVY", {})

        writer.writerow([
            target_date,
            total_requests, round(total_cost, 4),
            light.get("requests", 0), light.get("est_tokens", 0), round(light.get("est_cost", 0.0), 4),
            medium.get("requests", 0), medium.get("est_tokens", 0), round(medium.get("est_cost", 0.0), 4),
            heavy.get("requests", 0), heavy.get("est_tokens", 0), round(heavy.get("est_cost", 0.0), 4),
        ])

    # Print summary
    print(f"Usage for {target_date}:")
    print(f"  Total: {total_requests} requests, ~${total_cost:.4f}")
    for tier in ["LIGHT", "MEDIUM", "HEAVY"]:
        t = tier_summary.get(tier)
        if t and t["requests"] > 0:
            print(f"  {tier}: {t['requests']} requests, ~{t['est_tokens']} tokens, ~${t['est_cost']:.4f}")
    print(f"  Appended to {history_csv}")


def main():
    parser = argparse.ArgumentParser(
        description="Parse router logs and track daily API costs"
    )
    parser.add_argument(
        "date",
        nargs="?",
        default=None,
        help="Date to summarize (YYYY-MM-DD, default: today)",
    )
    parser.add_argument(
        "--routing-csv",
        default="routing.csv",
        help="Path to router's routing.csv (default: ./routing.csv)",
    )
    parser.add_argument(
        "--history-csv",
        default="usage-history.csv",
        help="Path to output history CSV (default: ./usage-history.csv)",
    )
    args = parser.parse_args()

    run(
        routing_csv=args.routing_csv,
        history_csv=args.history_csv,
        target_date=args.date,
    )


if __name__ == "__main__":
    main()
