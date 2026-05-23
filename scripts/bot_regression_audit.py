#!/usr/bin/env python3
"""Offline regression audit for the active Xiaowei, Daisy, Nikita, BUBU, and Nixie bots.

This script intentionally reads local config/state/log files only. It does not
send messages to LINE or WeChat, mutate bot memory, or call external services.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable


HERMES_ROOT = Path("/Users/vc/codex/hermes-agent-latest")
XIAOWEI_PROFILE = Path("/Users/vc/.hermes/profiles/wechat")
DAISY_PROFILE = Path("/Users/vc/.hermes/profiles/daisy")
NIKITA_PROFILE = Path("/Users/vc/.hermes/profiles/nikita")
BUBU_HERMES_HOME = Path("/Users/vc/.hermes-bubu")
BUBU_RUNTIME_ROOT = Path("/Users/vc/Library/Application Support/bubu-line-agent")
BUBU_PROXY_SCRIPT = Path("/Users/vc/.openclaw/scripts/hermes-bubu-line-proxy.py")
NIXIE_RUNTIME = Path("/Users/vc/Library/Application Support/nixie-lab-agent/runtime")
NIXIE_LAB_SOURCE = Path("/Users/vc/nixie-lab")

STATUS_RANK = {"ok": 0, "skip": 0, "warn": 1, "fail": 2}


SYSTEM_LEAK_PATTERNS = [
    ("self-improvement", re.compile(r"Self-improvement review", re.I)),
    ("dangerous-approval", re.compile(r"Dangerous command requires approval", re.I)),
    ("approval-ack", re.compile(r"Command approved|agent is resuming", re.I)),
    ("working-status", re.compile(r"Still working|Interrupting current task|iteration\s+\d+/\d+", re.I)),
    ("cron-blocked", re.compile(r"Cron job .*failed|prompt matches threat pattern|deception_hide", re.I)),
    ("tool-status", re.compile(r"running:\s+[a-z_]+|requires approval", re.I)),
    ("raw-command-status", re.compile(r"\bexit code\s+\d+\b|Traceback \(most recent call last\)", re.I)),
    ("provider-limit", re.compile(r"CLI error|hit your limit|resets?\s+.*Asia/Taipei", re.I)),
]

DAISY_CONTEXT_MISS_PATTERNS = [
    ("image-context-miss", re.compile(r"沒看到|看不到|再丟一次|上一張圖|上一段文字|我這邊只看到|沒有看到")),
    ("reply-context-miss", re.compile(r"是哪個|哪一件|內容丟給我|你把內容再貼一次")),
]

NIXIE_FORMAT_PATTERNS = [
    ("dense-numbering", re.compile(r"\b1\..{60,}\b2\.", re.S)),
    ("system-plan-leak", re.compile(r"改進計劃草稿|improvement-plan-|要看完整內容用", re.I)),
]

HERMES_LINE_GROUP_NEEDLES = [
    "_message_mentions_self",
    "_group_trigger_allowed",
    "_group_chime_score",
    "_remember_group_media",
    "_sent_chat_media_for_quote",
    "_incoming_message_text_for_quote",
    "mention_keyword_or_chime",
]

BUBU_GROUP_NEEDLES = [
    "GROUP_REPLY_MODE",
    "GROUP_CHIME_IN_ENABLED",
    "_message_mentions_self",
    "_group_trigger_allowed",
    "_group_chime_score",
    "_strip_group_trigger",
]

NIXIE_GROUP_NEEDLES = [
    "_should_reply_to_event",
    "_message_mentions_self",
    "_find_group_trigger_keyword",
    "_record_group_chime",
    "mention_keyword_or_chime",
]


@dataclasses.dataclass
class Check:
    status: str
    name: str
    detail: str
    evidence: list[str] = dataclasses.field(default_factory=list)


@dataclasses.dataclass
class BotReport:
    bot: str
    checks: list[Check]

    @property
    def status(self) -> str:
        rank = max((STATUS_RANK.get(check.status, 0) for check in self.checks), default=0)
        if rank >= 2:
            return "fail"
        if rank == 1:
            return "warn"
        return "ok"


def check(status: str, name: str, detail: str, evidence: Iterable[str] = ()) -> Check:
    return Check(status=status, name=name, detail=detail, evidence=list(evidence))


def read_text(path: Path, max_bytes: int = 1_000_000) -> str:
    try:
        with path.open("rb") as handle:
            data = handle.read(max_bytes)
    except OSError:
        return ""
    return data.decode("utf-8", errors="replace")


def load_json(path: Path) -> object | None:
    try:
        return json.loads(read_text(path, max_bytes=5_000_000))
    except json.JSONDecodeError:
        return None


def unique_existing(paths: Iterable[Path]) -> list[Path]:
    seen: set[Path] = set()
    out: list[Path] = []
    for path in paths:
        try:
            resolved = path.resolve()
        except OSError:
            resolved = path
        if resolved in seen or not path.exists() or not path.is_file():
            continue
        seen.add(resolved)
        out.append(path)
    return out


def recent_files(
    base: Path,
    patterns: Iterable[str],
    max_files: int = 30,
    since_mtime: float | None = None,
) -> list[Path]:
    if not base.exists():
        return []
    found: list[Path] = []
    for pattern in patterns:
        found.extend(base.glob(pattern))
    files = unique_existing(found)
    if since_mtime is not None:
        files = [path for path in files if path.stat().st_mtime >= since_mtime]
    files.sort(key=lambda path: path.stat().st_mtime, reverse=True)
    return files[:max_files]


def scan_patterns(files: Iterable[Path], patterns: list[tuple[str, re.Pattern[str]]]) -> dict[str, dict[str, object]]:
    summary: dict[str, dict[str, object]] = {
        name: {"count": 0, "examples": []} for name, _ in patterns
    }
    for path in files:
        text = read_text(path, max_bytes=700_000)
        if not text:
            continue
        for name, pattern in patterns:
            matches = pattern.findall(text)
            if not matches:
                continue
            bucket = summary[name]
            bucket["count"] = int(bucket["count"]) + len(matches)
            examples = bucket["examples"]
            if isinstance(examples, list) and len(examples) < 3:
                examples.append(str(path))
    return summary


def summarize_scan(
    name: str,
    files: list[Path],
    patterns: list[tuple[str, re.Pattern[str]]],
    clean_detail: str,
    warn_detail: str,
) -> Check:
    if not files:
        return check("skip", name, "No recent files were available to scan.")
    results = scan_patterns(files, patterns)
    hits = {
        pattern_name: data
        for pattern_name, data in results.items()
        if isinstance(data.get("count"), int) and int(data["count"]) > 0
    }
    if not hits:
        return check("ok", name, clean_detail, [f"scanned {len(files)} files"])
    evidence = []
    for pattern_name, data in hits.items():
        examples = data.get("examples")
        sample = ""
        if isinstance(examples, list) and examples:
            sample = f" e.g. {examples[0]}"
        evidence.append(f"{pattern_name}: {data['count']}{sample}")
    return check("warn", name, warn_detail, evidence)


def launchd_state(label: str) -> Check:
    command = ["launchctl", "print", f"gui/{os.getuid()}/{label}"]
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=3)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return check("warn", "launchd", f"Could not query launchd label {label}.", [str(exc)])
    if result.returncode != 0:
        return check("fail", "launchd", f"LaunchAgent {label} is not loaded.", [result.stderr.strip()])
    text = result.stdout
    if "state = running" in text:
        return check("ok", "launchd", f"LaunchAgent {label} is running.")
    return check("warn", "launchd", f"LaunchAgent {label} is loaded but not running.")


def local_health(port: int, path: str = "/healthz") -> Check:
    url = f"http://127.0.0.1:{port}{path}"
    command = ["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN"]
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=3)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return check("warn", "local health", f"Could not query local listener for {url}.", [str(exc)])
    if result.returncode == 0:
        return check("ok", "local health", f"Local service is listening for {url}.", [result.stdout.splitlines()[-1]])
    return check(
        "warn",
        "local health",
        f"Local service is not listening for {url}.",
        [(result.stderr or result.stdout).strip()[:240]],
    )


def config_contains(profile: Path, expected: list[str]) -> Check:
    config = read_text(profile / "config.yaml")
    missing = [item for item in expected if item not in config]
    if missing:
        return check("warn", "system-notice config", "Some suppression settings are missing.", missing)
    return check("ok", "system-notice config", "System/progress notice suppression settings are present.")


def env_value(profile: Path, key: str) -> str:
    env_text = read_text(profile / ".env")
    for raw_line in env_text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        env_key, value = line.split("=", 1)
        if env_key.strip() == key:
            return value.strip()
    return ""


def line_group_contract_check() -> Check:
    adapter = HERMES_ROOT / "plugins" / "platforms" / "line" / "adapter.py"
    text = read_text(adapter)
    missing = [needle for needle in HERMES_LINE_GROUP_NEEDLES if needle not in text]
    if missing:
        return check("warn", "LINE group contract", "Hermes LINE adapter is missing group-chat primitives.", missing)
    return check(
        "ok",
        "LINE group contract",
        "Hermes LINE adapter supports mention, quote, media context, and conservative chime-in gates.",
    )


def line_group_profile_check(profile: Path, bot_name: str, *, required: bool = False) -> Check:
    allowed_groups = env_value(profile, "LINE_ALLOWED_GROUPS")
    trigger_keywords = env_value(profile, "LINE_GROUP_TRIGGER_KEYWORDS")
    reply_mode = env_value(profile, "LINE_GROUP_REPLY_MODE")
    chime_enabled = env_value(profile, "LINE_GROUP_CHIME_IN_ENABLED")
    if not allowed_groups:
        status = "warn" if required else "skip"
        return check(status, "LINE group profile", f"{bot_name} has no allowed LINE group configured.")
    problems = []
    if not trigger_keywords:
        problems.append("LINE_GROUP_TRIGGER_KEYWORDS is empty")
    if reply_mode not in {"mention_or_keyword", "mention_keyword_or_chime", "chime_only"}:
        problems.append(f"LINE_GROUP_REPLY_MODE={reply_mode or '<missing>'}")
    if reply_mode in {"mention_keyword_or_chime", "chime_only"} and chime_enabled.lower() not in {"1", "true", "yes", "on"}:
        problems.append("LINE_GROUP_CHIME_IN_ENABLED is not true")
    if problems:
        return check("warn", "LINE group profile", f"{bot_name} group profile is incomplete.", problems)
    return check(
        "ok",
        "LINE group profile",
        f"{bot_name} is configured for allowed LINE groups with mention/keyword/chime gating.",
        [f"allowed_group_count={len([item for item in allowed_groups.split(',') if item.strip()])}"],
    )


def channel_directory_group_check(profile: Path, bot_name: str) -> Check:
    data = load_json(profile / "channel_directory.json")
    if not isinstance(data, dict):
        return check("skip", "LINE group directory", f"{bot_name} channel directory is unavailable.")
    line_items = ((data.get("platforms") or {}).get("line") or []) if isinstance(data.get("platforms"), dict) else []
    group_count = sum(1 for item in line_items if isinstance(item, dict) and item.get("type") == "group")
    if group_count:
        return check("ok", "LINE group directory", f"{bot_name} has {group_count} LINE group channel(s) recorded.")
    return check("skip", "LINE group directory", f"{bot_name} has no recorded LINE group channel yet.")


def xiaowei_report(since_mtime: float | None = None) -> BotReport:
    checks: list[Check] = []
    checks.append(check("ok" if XIAOWEI_PROFILE.exists() else "fail", "profile", str(XIAOWEI_PROFILE)))
    checks.append(launchd_state("ai.hermes.gateway-wechat"))
    checks.append(
        config_contains(
            XIAOWEI_PROFILE,
            [
                "interim_assistant_messages: false",
                "status_messages: false",
                "tool_progress: false",
                "busy_ack_enabled: false",
                "gateway_notify_interval: 0",
                "cron:\n  wrap_response: false",
            ],
        )
    )

    todos_path = XIAOWEI_PROFILE / "data" / "todos.json"
    todos = load_json(todos_path)
    if not isinstance(todos, dict):
        checks.append(check("fail", "durable todos", "Could not read Xiaowei durable todo store.", [str(todos_path)]))
    else:
        items = todos.get("items", [])
        open_items = [
            item for item in items
            if isinstance(item, dict) and item.get("status", "open") == "open"
        ]
        status = "ok" if len(open_items) >= 3 else "warn"
        checks.append(check(status, "durable todos", f"Durable todo store has {len(open_items)} open items.", [str(todos_path)]))

    cron_path = XIAOWEI_PROFILE / "cron" / "jobs.json"
    jobs = load_json(cron_path)
    todo_job = None
    if isinstance(jobs, dict):
        for job in jobs.get("jobs", []):
            if isinstance(job, dict) and job.get("script") == "daily_todo_digest.py":
                todo_job = job
                break
    if not isinstance(todo_job, dict):
        checks.append(check("fail", "todo cron", "Daily todo digest cron job was not found.", [str(cron_path)]))
    else:
        problems = []
        if not todo_job.get("enabled"):
            problems.append("job disabled")
        if not todo_job.get("no_agent"):
            problems.append("no_agent is not true")
        if todo_job.get("enabled_toolsets") not in ([], None):
            problems.append("enabled_toolsets is not empty")
        if problems:
            checks.append(check("warn", "todo cron", "Daily todo digest exists but is not fully deterministic.", problems))
        else:
            checks.append(check("ok", "todo cron", "Daily todo digest is deterministic and enabled."))

    gateway_text = read_text(HERMES_ROOT / "gateway" / "run.py")
    gateway_needles = [
        "_is_xiaowei_todo_query",
        "_format_xiaowei_todos",
        "_add_xiaowei_todo",
        "_handle_xiaowei_todo_batch",
        "_sync_xiaowei_todos_from_agent_messages",
        'r"^待辦\\s+(.+)$"',
    ]
    missing_gateway = [needle for needle in gateway_needles if needle not in gateway_text]
    if missing_gateway:
        checks.append(check("warn", "todo shortcut", "Gateway todo shortcut is incomplete.", missing_gateway))
    else:
        checks.append(check("ok", "todo shortcut", "Gateway has direct Xiaowei todo query/add handling."))

    files = recent_files(XIAOWEI_PROFILE / "sessions", ["*.jsonl"], max_files=25, since_mtime=since_mtime)
    checks.append(
        summarize_scan(
            "system leak scan",
            files,
            SYSTEM_LEAK_PATTERNS,
            "No system/progress leak patterns found in recent Xiaowei files.",
            "Recent Xiaowei files still contain system/progress leak patterns.",
        )
    )
    return BotReport("xiaowei", checks)


def daisy_report(since_mtime: float | None = None) -> BotReport:
    checks: list[Check] = []
    checks.append(check("ok" if DAISY_PROFILE.exists() else "fail", "profile", str(DAISY_PROFILE)))
    checks.append(launchd_state("ai.hermes.gateway-daisy"))
    checks.append(
        config_contains(
            DAISY_PROFILE,
            [
                "interim_assistant_messages: false",
                "status_messages: false",
                "approval_prompt_style: compact",
                "approval_numeric_shortcuts: true",
                "approval_ack_enabled: false",
                "tool_progress: false",
                "busy_ack_enabled: false",
                "gateway_notify_interval: 0",
            ],
        )
    )
    checks.append(line_group_contract_check())
    checks.append(line_group_profile_check(DAISY_PROFILE, "Daisy", required=True))
    checks.append(channel_directory_group_check(DAISY_PROFILE, "Daisy"))

    plugin_paths = [
        DAISY_PROFILE / "plugins" / "adult-fiction-router",
        DAISY_PROFILE / "adult-fiction-router",
    ]
    config = read_text(DAISY_PROFILE / "config.yaml")
    if "adult-fiction-router" in config and any(path.exists() for path in plugin_paths):
        checks.append(check("ok", "adult router", "Adult fiction router is enabled and present."))
    else:
        checks.append(check("warn", "adult router", "Adult fiction router is not fully enabled or missing."))

    context_files = [
        DAISY_PROFILE / "line-incoming-context.json",
        DAISY_PROFILE / "line-sent-media-context.json",
    ]
    missing_context = [str(path) for path in context_files if not path.exists()]
    if missing_context:
        checks.append(check("warn", "line context", "Some LINE context files are missing.", missing_context))
    else:
        checks.append(check("ok", "line context", "LINE incoming/replied media context files exist."))

    memory_files = [
        DAISY_PROFILE / "memories" / "MEMORY.md",
        DAISY_PROFILE / "memories" / "USER.md",
    ]
    missing_memory = [str(path) for path in memory_files if not path.exists()]
    if missing_memory:
        checks.append(check("warn", "memory", "Some Daisy memory files are missing.", missing_memory))
    else:
        sizes = [f"{path.name}: {path.stat().st_size} bytes" for path in memory_files]
        checks.append(check("ok", "memory", "Daisy memory files exist.", sizes))

    files = recent_files(DAISY_PROFILE / "sessions", ["*.jsonl"], max_files=30, since_mtime=since_mtime)
    checks.append(
        summarize_scan(
            "system leak scan",
            files,
            SYSTEM_LEAK_PATTERNS,
            "No system/progress leak patterns found in recent Daisy files.",
            "Recent Daisy files still contain system/progress leak patterns.",
        )
    )
    checks.append(
        summarize_scan(
            "context miss scan",
            files,
            DAISY_CONTEXT_MISS_PATTERNS,
            "No obvious image/reply context misses found in recent Daisy files.",
            "Recent Daisy files show image/reply context misses.",
        )
    )
    return BotReport("daisy", checks)


def nikita_report(since_mtime: float | None = None) -> BotReport:
    checks: list[Check] = []
    checks.append(check("ok" if NIKITA_PROFILE.exists() else "fail", "profile", str(NIKITA_PROFILE)))
    checks.append(launchd_state("ai.hermes.gateway-nikita"))
    checks.append(local_health(18795, "/line/webhook/health"))
    checks.append(
        config_contains(
            NIKITA_PROFILE,
            [
                "interim_assistant_messages: false",
                "status_messages: false",
                "approval_prompt_style: compact",
                "approval_numeric_shortcuts: true",
                "approval_ack_enabled: false",
                "tool_progress: false",
                "busy_ack_enabled: false",
                "gateway_notify_interval: 0",
            ],
        )
    )
    checks.append(line_group_contract_check())
    checks.append(line_group_profile_check(NIKITA_PROFILE, "Nikita"))
    checks.append(channel_directory_group_check(NIKITA_PROFILE, "Nikita"))

    memory_files = [
        NIKITA_PROFILE / "memories" / "MEMORY.md",
        NIKITA_PROFILE / "memories" / "USER.md",
    ]
    missing_memory = [str(path) for path in memory_files if not path.exists()]
    if missing_memory:
        checks.append(check("warn", "memory", "Some Nikita memory files are missing.", missing_memory))
    else:
        sizes = [f"{path.name}: {path.stat().st_size} bytes" for path in memory_files]
        checks.append(check("ok", "memory", "Nikita memory files exist.", sizes))

    files = recent_files(NIKITA_PROFILE / "sessions", ["*.jsonl"], max_files=25, since_mtime=since_mtime)
    checks.append(
        summarize_scan(
            "system leak scan",
            files,
            SYSTEM_LEAK_PATTERNS,
            "No system/progress leak patterns found in recent Nikita files.",
            "Recent Nikita files still contain system/progress leak patterns.",
        )
    )
    return BotReport("nikita", checks)


def bubu_report(since_mtime: float | None = None) -> BotReport:
    checks: list[Check] = []
    checks.append(check("ok" if BUBU_HERMES_HOME.exists() else "fail", "hermes home", str(BUBU_HERMES_HOME)))
    checks.append(check("ok" if BUBU_RUNTIME_ROOT.exists() else "warn", "runtime root", str(BUBU_RUNTIME_ROOT)))
    checks.append(check("ok" if BUBU_PROXY_SCRIPT.exists() else "fail", "line proxy", str(BUBU_PROXY_SCRIPT)))
    checks.append(launchd_state("com.openclaw.hermes-bubu-line-proxy"))
    checks.append(local_health(18794, "/healthz"))

    script_text = read_text(BUBU_PROXY_SCRIPT)
    needed_filters = [
        "suppress_system_status_text",
        "No response from provider",
        "Retrying in",
        "Still working",
        "Interrupting current task",
        "Command approved",
        "Dangerous command requires approval",
        "Self-improvement review",
        "Cron job .*failed",
        "CLI error",
        "hit your limit",
    ]
    missing_filters = [needle for needle in needed_filters if needle not in script_text]
    if missing_filters:
        checks.append(check("warn", "system-status filter", "BUBU bridge is missing some status suppression patterns.", missing_filters))
    else:
        checks.append(check("ok", "system-status filter", "BUBU bridge filters Hermes/Codex status output before LINE delivery."))

    missing_group = [needle for needle in BUBU_GROUP_NEEDLES if needle not in script_text]
    if missing_group:
        checks.append(check("warn", "group trigger contract", "BUBU bridge is missing group-chat trigger primitives.", missing_group))
    else:
        checks.append(check("ok", "group trigger contract", "BUBU bridge supports keyword, native mention, and conservative group chime gates."))

    files = (
        recent_files(BUBU_HERMES_HOME / "sessions", ["*.jsonl"], max_files=20, since_mtime=since_mtime)
        + recent_files(BUBU_RUNTIME_ROOT / "data", ["*.jsonl"], max_files=20, since_mtime=since_mtime)
    )
    checks.append(
        summarize_scan(
            "system leak scan",
            files,
            SYSTEM_LEAK_PATTERNS,
            "No system/progress leak patterns found in recent BUBU files.",
            "Recent BUBU files still contain system/progress leak patterns.",
        )
    )
    return BotReport("bubu", checks)


def nixie_port_from_secrets() -> int | None:
    secrets = load_json(NIXIE_RUNTIME / ".secrets")
    if not isinstance(secrets, dict):
        return None
    port = secrets.get("line_agent_port")
    if isinstance(port, int):
        return port
    if isinstance(port, str) and port.isdigit():
        return int(port)
    return None


def source_fix_check(label: str, path: Path, needles: list[str]) -> Check:
    text = read_text(path)
    missing = [needle for needle in needles if needle not in text]
    if missing:
        return check("warn", label, f"{path} is missing expected implementation markers.", missing)
    return check("ok", label, f"{path} contains expected implementation markers.")


def nixie_report(since_mtime: float | None = None) -> BotReport:
    checks: list[Check] = []
    checks.append(check("ok" if NIXIE_RUNTIME.exists() else "fail", "runtime", str(NIXIE_RUNTIME)))
    checks.append(check("ok" if NIXIE_LAB_SOURCE.exists() else "warn", "lab source", str(NIXIE_LAB_SOURCE)))
    checks.append(launchd_state("com.vc.nixie-lab-codex-line-proxy"))

    required_state = [
        NIXIE_RUNTIME / ".secrets",
        NIXIE_RUNTIME / "MEMORY.md",
        NIXIE_RUNTIME / "data" / "personal-organizer.json",
    ]
    missing = [str(path) for path in required_state if not path.exists()]
    if missing:
        checks.append(check("fail", "runtime state", "Required Nixie runtime state files are missing.", missing))
    else:
        checks.append(check("ok", "runtime state", "Required Nixie runtime state files exist."))

    port = nixie_port_from_secrets()
    if port:
        checks.append(local_health(port))
    else:
        checks.append(check("warn", "local health", "Could not determine Nixie local port from runtime .secrets."))

    fix_needles = [
        "_schedule_task_implies_morning",
        "_with_morning_context_for_ambiguous_time",
    ]
    checks.append(source_fix_check("source morning-time fix", NIXIE_LAB_SOURCE / "nixie_organizer.py", fix_needles))
    checks.append(source_fix_check("runtime morning-time fix", NIXIE_RUNTIME / "nixie_organizer.py", fix_needles))

    checks.append(source_fix_check("source group routing", NIXIE_LAB_SOURCE / "codex_line_proxy.py", NIXIE_GROUP_NEEDLES))
    checks.append(source_fix_check("runtime group routing", NIXIE_RUNTIME / "codex_line_proxy.py", NIXIE_GROUP_NEEDLES))
    secrets = load_json(NIXIE_RUNTIME / ".secrets")
    if isinstance(secrets, dict):
        group_mode = str(secrets.get("group_reply_mode") or "")
        chime_enabled = bool(secrets.get("group_chime_in_enabled"))
        keyword_count = len(secrets.get("group_trigger_keywords") or [])
        if group_mode == "mention_keyword_or_chime" and chime_enabled and keyword_count:
            checks.append(
                check(
                    "ok",
                    "runtime group profile",
                    "Nixie runtime has mention/keyword/chime group mode enabled.",
                    [f"keyword_count={keyword_count}"],
                )
            )
        else:
            checks.append(
                check(
                    "warn",
                    "runtime group profile",
                    "Nixie runtime group settings are incomplete.",
                    [f"group_reply_mode={group_mode or '<missing>'}", f"group_chime_in_enabled={chime_enabled}", f"keyword_count={keyword_count}"],
                )
            )
    else:
        checks.append(check("warn", "runtime group profile", "Could not read Nixie runtime group settings."))

    files = (
        recent_files(NIXIE_RUNTIME / "data", ["*.jsonl"], max_files=20, since_mtime=since_mtime)
        + recent_files(NIXIE_RUNTIME / "runs", ["*.jsonl", "**/*.jsonl"], max_files=20, since_mtime=since_mtime)
    )
    checks.append(
        summarize_scan(
            "system leak scan",
            files,
            SYSTEM_LEAK_PATTERNS + NIXIE_FORMAT_PATTERNS,
            "No system/progress/plan leak patterns found in recent Nixie files.",
            "Recent Nixie files still contain system/progress/plan leak patterns.",
        )
    )
    return BotReport("nixie", checks)


def build_reports(bot_filter: str | None, since_mtime: float | None = None) -> list[BotReport]:
    factories = {
        "xiaowei": xiaowei_report,
        "daisy": daisy_report,
        "nikita": nikita_report,
        "bubu": bubu_report,
        "nixie": nixie_report,
    }
    if bot_filter:
        return [factories[bot_filter](since_mtime)]
    return [factory(since_mtime) for factory in factories.values()]


def overall_status(reports: list[BotReport]) -> str:
    rank = max((STATUS_RANK.get(report.status, 0) for report in reports), default=0)
    if rank >= 2:
        return "fail"
    if rank == 1:
        return "warn"
    return "ok"


def improvement_plan(reports: list[BotReport]) -> list[str]:
    plan: list[str] = []
    for report in reports:
        for item in report.checks:
            if item.status not in {"warn", "fail"}:
                continue
            prefix = "P0" if item.status == "fail" else "P1"
            plan.append(f"{prefix} {report.bot}: {item.name} - {item.detail}")
    if not plan:
        plan.append("No immediate fixes. Keep this audit in the pre/post-change checklist.")
    return plan


def reports_to_json(reports: list[BotReport], since_mtime: float | None = None) -> str:
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "since_mtime": since_mtime,
        "overall": overall_status(reports),
        "reports": [
            {
                "bot": report.bot,
                "status": report.status,
                "checks": [dataclasses.asdict(item) for item in report.checks],
            }
            for report in reports
        ],
        "improvement_plan": improvement_plan(reports),
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


def print_markdown(reports: list[BotReport], since_mtime: float | None = None) -> None:
    print(f"Bot regression audit - overall: {overall_status(reports).upper()}")
    if since_mtime is not None:
        since = datetime.fromtimestamp(since_mtime, tz=timezone.utc).astimezone().isoformat()
        print(f"Since: {since}")
    print()
    for report in reports:
        print(f"[{report.bot}] {report.status.upper()}")
        for item in report.checks:
            print(f"- {item.status.upper()} {item.name}: {item.detail}")
            for evidence in item.evidence[:3]:
                print(f"  evidence: {evidence}")
        print()
    print("Improvement plan")
    for item in improvement_plan(reports):
        print(f"- {item}")


def parse_since(value: str | None) -> float | None:
    if not value:
        return None
    clean = value.strip()
    if clean == "now":
        return datetime.now().timestamp()
    if clean.isdigit():
        return float(clean)
    path = Path(clean).expanduser()
    if path.exists():
        return path.stat().st_mtime
    try:
        return datetime.fromisoformat(clean.replace("Z", "+00:00")).timestamp()
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            "--since must be 'now', a unix timestamp, an ISO datetime, or an existing file path"
        ) from exc


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bot", choices=["xiaowei", "daisy", "nikita", "bubu", "nixie"], help="Audit one bot only.")
    parser.add_argument(
        "--since",
        help="Only scan session/log files modified after this time: 'now', unix timestamp, ISO datetime, or file path.",
    )
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    since_mtime = parse_since(args.since)
    reports = build_reports(args.bot, since_mtime)
    if args.json:
        print(reports_to_json(reports, since_mtime))
    else:
        print_markdown(reports, since_mtime)
    return 1 if overall_status(reports) == "fail" else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
