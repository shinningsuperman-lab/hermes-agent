# Bot Regression Self-Test - 2026-05-22

Tester: Codex
Timezone: Asia/Taipei

## Baseline

Command:

```bash
python3 scripts/bot_regression_audit.py
```

Result: WARN

Findings:

- Xiaowei: current config/todo checks OK; historical system/progress leak patterns still exist in old sessions/logs.
- Daisy: current config/adult-router/context-file checks OK; historical system/progress leaks and image/reply misses still exist in old sessions/logs.
- Nixie: live runtime healthy, but production runtime was missing lab source morning/Taipei scheduling helpers.

## Fixes Applied

### Nixie Runtime Scheduling Drift

Changed live runtime file:

```text
/Users/vc/Library/Application Support/nixie-A-line-agent/runtime/nixie_organizer.py
```

Source copied from:

```text
/Users/vc/nixie-lab/nixie_organizer.py
```

Backup saved at:

```text
/Users/vc/Library/Application Support/nixie-A-line-agent/upgrade-backups/20260522-selftest/nixie_organizer.py.before-selftest
```

Validation before deploy:

```bash
python3 -m py_compile /Users/vc/nixie-lab/nixie_organizer.py
python3 -m pytest /Users/vc/nixie-lab/tests/test_nixie_organizer.py
```

Result:

```text
21 passed
```

Validation after deploy:

```bash
python3 -m py_compile "/Users/vc/Library/Application Support/nixie-A-line-agent/runtime/nixie_organizer.py"
launchctl kickstart -k gui/501/com.vc.nixie-codex-line-proxy
python3 scripts/bot_regression_audit.py --bot nixie
```

Result: Nixie OK

### Audit Fresh-Run Filtering

Updated:

```text
scripts/bot_regression_audit.py
BOT_REGRESSION_RUNBOOK.md
```

New usage:

```bash
python3 scripts/bot_regression_audit.py --since now
python3 scripts/bot_regression_audit.py --since 2026-05-22T17:00:00+08:00
```

Purpose:

- Separate current regression results from historical logs.
- Keep old Xiaowei/Daisy evidence visible in normal audit.
- Let a new live-safe test round fail only on fresh output.

Validation:

```bash
python3 -m py_compile scripts/bot_regression_audit.py
python3 scripts/bot_regression_audit.py --since now
```

Result: OK

### Automated Self-Test Pack

Added:

```text
scripts/bot_regression_selftest.py
```

Current command:

```bash
python3 scripts/bot_regression_selftest.py
```

Current result: PASS

Included checks:

- Hermes regression scripts compile.
- Daisy/LINE adapter and gateway tests: 109 passed.
- Nixie organizer unit tests: 21 passed.
- Nixie runtime audit: OK.
- Xiaowei durable todo audit with fresh-run filtering: OK.

## Remaining Work

Next practical tests:

1. Daisy P0 live-observed cases: image context, quoted group context, group trigger.
2. Nixie P0 live-safe cases: finance output formatting and time reminder behavior.
3. Xiaowei P0 live-safe cases: durable todo query and system notice filtering after current timestamp.

Use:

```bash
python3 scripts/bot_regression_plan.py --bot daisy --priority P0
python3 scripts/bot_regression_plan.py --bot nixie --priority P0
python3 scripts/bot_regression_plan.py --bot xiaowei --priority P0
```

## Follow-Up Fix: Xiaowei Todo Split-Brain

Observed issue:

- User sent `待辦 數字化的 MPS 要研究一下` and `待辦 下週要跟值日生 改一下 lenovo系統量`.
- Xiaowei replied as if saved, but `待辦更新` still showed the old durable list.

Root cause:

- Gateway shortcut handled `待辦：內容` and `待辦事項 內容`, but not `待辦 空格 內容`.
- Those messages fell through to the old agent todo tool, while durable queries read `data/todos.json`.

Fix:

- `gateway/run.py` now handles `待辦 空格 內容` directly.
- `gateway/run.py` also bridges accidental old `todo` tool writes into durable `data/todos.json` when the original Xiaowei message has todo intent.
- `scripts/bot_regression_audit.py` checks that this shortcut remains present.
- `docs/bot-regression-cases.json` now tests the space format.
- `todo_store.py` and gateway todo writes now use unique temp files to avoid temp-file races.

Data repair:

- Durable Xiaowei todos now have 13 open items.
- Added missing items:
  - 調整 7月的 UTS 到 1.2M
  - 數字化的 MPS 要研究一下
  - 下週要跟值日生改一下 Lenovo 系統量

Validation:

```bash
python3 /Users/vc/.hermes/profiles/wechat/scripts/todo_store.py list
/Users/vc/codex/hermes-agent-latest/venv/bin/python -m pytest -o addopts= tests/gateway/test_xiaowei_todo_bridge.py
python3 scripts/bot_regression_audit.py --bot xiaowei --since now
python3 scripts/bot_regression_selftest.py --bot xiaowei
```

Result: PASS

## Follow-Up Fix: Xiaowei Multi-Line Todo Bubble

Observed issue:

- User sent one WeChat bubble with:

```text
待辦 測試 durable bridge 001
待辦更新
今日待辦呢？
```

- The durable store was updated to 14 items by the safety bridge, but the visible reply still came from the old in-session todo list and said 6 items.

Fix:

- Added `_handle_xiaowei_todo_batch()` in `gateway/run.py`.
- Xiaowei now parses todo chat bubbles line by line.
- A mixed add/query bubble is handled before the agent loop and replies from durable `data/todos.json`.

Regression:

- `tests/gateway/test_xiaowei_todo_bridge.py` now includes the exact multi-line bubble case.

Validation:

```bash
/Users/vc/codex/hermes-agent-latest/venv/bin/python -m pytest -o addopts= tests/gateway/test_xiaowei_todo_bridge.py
python3 scripts/bot_regression_selftest.py --bot xiaowei
```

Result: PASS, 4 tests passed.
