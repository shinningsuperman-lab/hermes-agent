#!/usr/bin/env python3
"""Render practical bot regression test cases as a manual test plan."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
CASE_FILE = REPO_ROOT / "docs" / "bot-regression-cases.json"


def load_cases(path: Path = CASE_FILE) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict) or not isinstance(data.get("cases"), list):
        raise ValueError(f"Invalid case file: {path}")
    return data


def matches(case: dict[str, Any], bot: str | None, priority: str | None, mode: str | None) -> bool:
    if bot and bot not in case.get("bots", []):
        return False
    if priority and case.get("priority") != priority:
        return False
    if mode and case.get("mode") != mode:
        return False
    return True


def filtered_cases(data: dict[str, Any], bot: str | None, priority: str | None, mode: str | None) -> list[dict[str, Any]]:
    cases = [case for case in data["cases"] if isinstance(case, dict) and matches(case, bot, priority, mode)]
    priority_rank = {"P0": 0, "P1": 1, "P2": 2}
    cases.sort(key=lambda case: (priority_rank.get(str(case.get("priority")), 9), str(case.get("id"))))
    return cases


def as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def render_step(step: Any) -> str:
    if not isinstance(step, dict):
        return str(step)
    if "send" in step:
        return f"Send: `{step.get('send', '')}`"
    if "run" in step:
        return f"Run: `{step.get('run', '')}`"
    if "observe" in step:
        return f"Observe: {step.get('observe', '')}"
    if "action" in step:
        return f"Action: {step.get('action', '')}"
    return str(step)


def render_markdown(data: dict[str, Any], cases: list[dict[str, Any]], bot: str | None) -> str:
    target = bot or "all bots"
    lines = [
        f"# Practical Bot Regression Plan - {target}",
        "",
        f"- Case file: `{CASE_FILE}`",
        f"- Updated: `{data.get('updated_at', 'unknown')}`",
        f"- Timezone: `{data.get('timezone', 'unknown')}`",
        f"- Case count: `{len(cases)}`",
        "",
        "## Execution Rules",
        "",
        "- Start with `python3 scripts/bot_regression_audit.py` and save the result.",
        "- Run live cases only in private chat or approved test groups.",
        "- Capture a screenshot for every live case.",
        "- After live cases, run the audit again and compare new warnings.",
        "- Do not paste explicit adult content into repo files or shared logs.",
        "",
    ]
    modes = data.get("modes")
    if isinstance(modes, dict):
        lines.extend(["## Modes", ""])
        for mode_name, description in modes.items():
            lines.append(f"- `{mode_name}`: {description}")
        lines.append("")

    lines.extend(["## Cases", ""])
    for case in cases:
        lines.append(f"### {case.get('id')} - {case.get('title')}")
        lines.append("")
        lines.append(f"- Bots: `{', '.join(as_list(case.get('bots')))}`")
        lines.append(f"- Priority: `{case.get('priority')}`")
        lines.append(f"- Mode: `{case.get('mode')}`")
        setup = as_list(case.get("setup"))
        if setup:
            lines.append("- Setup:")
            for item in setup:
                lines.append(f"  - {item}")
        steps = as_list(case.get("steps"))
        if steps:
            lines.append("- Steps:")
            for idx, step in enumerate(steps, 1):
                lines.append(f"  {idx}. {render_step(step)}")
        expected = as_list(case.get("expected"))
        if expected:
            lines.append("- Expected:")
            for item in expected:
                lines.append(f"  - {item}")
        pass_criteria = as_list(case.get("pass_criteria"))
        if pass_criteria:
            lines.append("- Pass criteria:")
            for item in pass_criteria:
                lines.append(f"  - {item}")
        evidence = as_list(case.get("evidence"))
        if evidence:
            lines.append("- Evidence:")
            for item in evidence:
                lines.append(f"  - {item}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bot", choices=["xiaowei", "daisy", "nikita", "bubu", "nixie"], help="Render cases for one bot.")
    parser.add_argument("--priority", choices=["P0", "P1", "P2"], help="Render one priority only.")
    parser.add_argument("--mode", choices=["offline", "live-safe", "live-observed", "manual-boundary"], help="Render one mode only.")
    parser.add_argument("--json", action="store_true", help="Print filtered cases as JSON.")
    parser.add_argument("--count", action="store_true", help="Print only the number of matching cases.")
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    data = load_cases()
    cases = filtered_cases(data, args.bot, args.priority, args.mode)
    if args.count:
        print(len(cases))
        return 0
    if args.json:
        print(json.dumps({"cases": cases}, ensure_ascii=False, indent=2))
        return 0
    print(render_markdown(data, cases, args.bot))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
