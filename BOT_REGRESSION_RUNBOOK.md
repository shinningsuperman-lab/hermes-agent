# Bot Regression Runbook

這份 runbook 用來固定檢查小薇、Daisy、Nixie 的常見退化問題。預設流程只讀本機資料，不會對 LINE 或 WeChat 發訊息。

## 指令

```bash
python3 scripts/bot_regression_audit.py
python3 scripts/bot_regression_audit.py --json
python3 scripts/bot_regression_audit.py --bot xiaowei
python3 scripts/bot_regression_audit.py --bot daisy
python3 scripts/bot_regression_audit.py --bot nixie
python3 scripts/bot_regression_audit.py --since now
python3 scripts/bot_regression_audit.py --since 2026-05-22T17:00:00+08:00
python3 scripts/bot_regression_selftest.py
python3 scripts/bot_regression_selftest.py --bot daisy

python3 scripts/bot_regression_plan.py
python3 scripts/bot_regression_plan.py --bot daisy --priority P0
python3 scripts/bot_regression_plan.py --bot nixie --mode offline
```

`bot_regression_audit.py` 是現況檢查；`bot_regression_selftest.py` 是目前可自動跑的單元/離線測試包；`bot_regression_plan.py` 會從
`docs/bot-regression-cases.json` 產生可以照著做的實測清單。

## 檢查範圍

小薇：

- Gateway/WeChat 是否仍在跑。
- 系統進度、approval、cron 錯誤訊息是否被關掉。
- `data/todos.json` 是否是待辦事項唯一可信來源。
- 每日待辦 cron 是否用 `daily_todo_digest.py` 直接讀檔，不再丟給 agent 自己猜。
- 最近 session/log 是否出現 `Still working`、`Dangerous command requires approval`、`Cron job failed` 等不像真人的文字。

Daisy：

- Gateway/LINE 是否仍在跑。
- LINE 進度、approval、忙碌提示是否被關掉。
- `adult-fiction-router` 是否啟用且存在。
- LINE incoming/replied media context 檔是否存在，避免她看不到上一張圖或引用訊息。
- 最近 session/log 是否出現系統通知、approval ack、看不到圖片/引用內容等問題。

Nixie：

- Production runtime 是否存在且 LaunchAgent 正常。
- `.secrets`、`MEMORY.md`、`data/personal-organizer.json` 是否存在。
- 本機 health endpoint 是否能回應。
- `nixie-lab` source 與 production runtime 是否都有「早上/台北時間」排程修正。
- 最近 log/runtime data 是否出現系統通知、改進計畫草稿外洩、或太密的編號格式。

## 使用方式

1. 每次改 bot 之前先跑一次 audit，留下目前基準；若要看本輪新問題，記下開始時間並用 `--since`。
2. 用 `bot_regression_plan.py --bot <bot>` 產生該 bot 的實測清單。
3. 先跑 `P0`，再跑 `P1`；live case 只在私人聊天室或白名單測試群跑。
4. 每個 live case 截圖，必要時記下測試時間。
5. 改完後再跑一次 audit，看 warn/fail 是否減少。
6. `FAIL` 代表可能會影響 live bot；先修再部署。
7. `WARN` 通常是歷史 log、格式品質、或 production runtime 尚未同步 source。

## 實測回合

第一回合：不碰 live chat，只看機器狀態。

```bash
python3 scripts/bot_regression_selftest.py
python3 scripts/bot_regression_audit.py
date "+%Y-%m-%dT%H:%M:%S%z"
python3 scripts/bot_regression_plan.py --mode offline
```

第二回合：每隻 bot 跑 P0。

```bash
python3 scripts/bot_regression_plan.py --bot xiaowei --priority P0
python3 scripts/bot_regression_plan.py --bot daisy --priority P0
python3 scripts/bot_regression_plan.py --bot nixie --priority P0
```

第三回合：跑 P1，主要抓體驗品質。

```bash
python3 scripts/bot_regression_plan.py --bot xiaowei --priority P1
python3 scripts/bot_regression_plan.py --bot daisy --priority P1
python3 scripts/bot_regression_plan.py --bot nixie --priority P1
```

第四回合：修完後重跑 audit。若同一 case 連續兩次通過，再把它視為穩定。

```bash
python3 scripts/bot_regression_audit.py --since 2026-05-22T17:00:00+08:00
```

## 目前優先測試案例

- 待辦：問「待辦事項」「今日待辦呢？」必須回 durable store 的完整清單。
- 時區：美股財報/開盤提醒要用台北時間描述與排程。
- 系統通知：不能顯示 approval、cron blocked、still working、iteration 等系統字。
- 圖片引用：引用上一張圖或回覆照片時，要能拿到 LINE/WeChat 的 media context。
- 群聊：不用明確 @ 時，也要能判斷是不是在找她，但不要亂插話。
- 資料格式：股市、財報、醫療、行程資料要分段、重點加粗、避免一整坨文字。
