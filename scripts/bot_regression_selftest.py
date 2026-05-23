#!/usr/bin/env python3
"""Run the current practical self-test pack for the active bot fleet."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path


HERMES_ROOT = Path("/Users/vc/codex/hermes-agent-latest")
HERMES_PY = HERMES_ROOT / "venv" / "bin" / "python"
NIXIE_LAB = Path("/Users/vc/nixie-lab")


@dataclass
class StepResult:
    name: str
    command: list[str]
    returncode: int
    stdout_tail: str
    stderr_tail: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0


def python_for_hermes() -> str:
    return str(HERMES_PY if HERMES_PY.exists() else Path(sys.executable))


def tail(text: str, max_chars: int = 4000) -> str:
    if len(text) <= max_chars:
        return text.strip()
    return text[-max_chars:].strip()


def run_step(name: str, command: list[str], cwd: Path) -> StepResult:
    result = subprocess.run(command, cwd=str(cwd), capture_output=True, text=True)
    return StepResult(
        name=name,
        command=command,
        returncode=result.returncode,
        stdout_tail=tail(result.stdout),
        stderr_tail=tail(result.stderr),
    )


def build_steps(bot: str | None) -> list[tuple[str, list[str], Path]]:
    hermes_py = python_for_hermes()
    steps: list[tuple[str, list[str], Path]] = [
        (
            "Hermes regression scripts compile",
            [
                hermes_py,
                "-m",
                "py_compile",
                "scripts/bot_regression_audit.py",
                "scripts/bot_regression_plan.py",
                "scripts/bot_regression_selftest.py",
            ],
            HERMES_ROOT,
        ),
    ]
    if bot in (None, "daisy", "nikita"):
        steps.append(
            (
                "Hermes LINE adapter and gateway tests",
                [
                    hermes_py,
                    "-m",
                    "pytest",
                    "-o",
                    "addopts=",
                    "tests/plugins/test_line_adapter.py",
                    "tests/gateway/test_line_plugin.py",
                ],
                HERMES_ROOT,
            )
        )
    if bot in (None, "daisy"):
        steps.append(
            (
                "Daisy Hermes audit",
                [hermes_py, "scripts/bot_regression_audit.py", "--bot", "daisy", "--since", "now"],
                HERMES_ROOT,
            )
        )
    if bot in (None, "nikita"):
        steps.append(
            (
                "Nikita Hermes audit",
                [hermes_py, "scripts/bot_regression_audit.py", "--bot", "nikita"],
                HERMES_ROOT,
            )
        )
    if bot in (None, "bubu"):
        steps.extend(
            [
                (
                    "BUBU Nixie runtime syntax check",
                    [
                        sys.executable,
                        "-c",
                        (
                            "import ast,pathlib;"
                            "ast.parse(pathlib.Path('/Users/vc/Library/Application Support/bubu-line-agent/runtime/codex_line_proxy.py').read_text())"
                        ),
                    ],
                    HERMES_ROOT,
                ),
                (
                    "BUBU Nixie runtime contract",
                    [
                        sys.executable,
                        "-c",
                        (
                            "import pathlib;"
                            "text=pathlib.Path('/Users/vc/Library/Application Support/bubu-line-agent/runtime/codex_line_proxy.py').read_text();"
                            "needles=['_filter_user_visible_messages','_USER_VISIBLE_SYSTEM_STATUS_RE','CLI error','hit your limit','_should_reply_to_event','_message_mentions_self','_find_group_trigger_keyword','_record_group_chime','mention_keyword_or_chime'];"
                            "missing=[needle for needle in needles if needle not in text];"
                            "assert not missing, missing"
                        ),
                    ],
                    HERMES_ROOT,
                ),
                (
                    "BUBU Nixie runtime smoke check",
                    [
                        sys.executable,
                        "/Users/vc/nixie-lab/scripts/runtime_smoke_check.py",
                        "--instance",
                        "bubu|/Users/vc/Library/Application Support/bubu-line-agent/runtime|18794|https://bubu.shinningsuperman.com",
                        "--skip-local",
                        "--skip-public",
                        "--json",
                    ],
                    HERMES_ROOT,
                ),
                (
                    "BUBU shared-core audit",
                    [hermes_py, "scripts/bot_regression_audit.py", "--bot", "bubu"],
                    HERMES_ROOT,
                ),
            ]
        )
    if bot in (None, "nixie"):
        steps.extend(
            [
                (
                    "Nixie LINE runtime tests",
                    [
                        sys.executable,
                        "-m",
                        "pytest",
                        "tests/test_codex_line_proxy_improvement_plan.py",
                        "tests/test_codex_line_proxy_memory.py",
                        "tests/test_codex_line_proxy_route_kernel.py",
                        "tests/test_nixie_organizer.py",
                    ],
                    NIXIE_LAB,
                ),
                (
                    "Nixie runtime audit",
                    [hermes_py, "scripts/bot_regression_audit.py", "--bot", "nixie", "--since", "now"],
                    HERMES_ROOT,
                ),
            ]
        )
    if bot in (None, "xiaowei"):
        steps.extend(
            [
                (
                    "Xiaowei durable todo bridge tests",
                    [
                        hermes_py,
                        "-m",
                        "pytest",
                        "-o",
                        "addopts=",
                        "tests/gateway/test_xiaowei_todo_bridge.py",
                    ],
                    HERMES_ROOT,
                ),
                (
                    "Xiaowei durable todo audit",
                    [hermes_py, "scripts/bot_regression_audit.py", "--bot", "xiaowei", "--since", "now"],
                    HERMES_ROOT,
                ),
            ]
        )
    return steps


def print_markdown(results: list[StepResult]) -> None:
    overall = "PASS" if all(result.ok for result in results) else "FAIL"
    print(f"Bot regression self-test - {overall}")
    print()
    for result in results:
        status = "PASS" if result.ok else "FAIL"
        print(f"- {status} {result.name}")
        print(f"  command: `{' '.join(result.command)}`")
        if result.stdout_tail:
            first_line = result.stdout_tail.splitlines()[-1] if result.stdout_tail.splitlines() else result.stdout_tail
            print(f"  output: {first_line}")
        if result.stderr_tail:
            print(f"  stderr: {result.stderr_tail.splitlines()[-1]}")


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bot", choices=["xiaowei", "daisy", "nikita", "bubu", "nixie"], help="Run one bot's self-test subset.")
    parser.add_argument("--json", action="store_true", help="Print JSON results.")
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    started_at = datetime.now().astimezone().isoformat()
    results = [run_step(name, command, cwd) for name, command, cwd in build_steps(args.bot)]
    if args.json:
        print(
            json.dumps(
                {
                    "started_at": started_at,
                    "overall": "pass" if all(result.ok for result in results) else "fail",
                    "results": [asdict(result) for result in results],
                },
                ensure_ascii=False,
                indent=2,
            )
        )
    else:
        print_markdown(results)
    return 0 if all(result.ok for result in results) else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
