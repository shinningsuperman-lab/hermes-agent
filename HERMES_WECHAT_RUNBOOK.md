# Hermes WeChat Runtime Runbook

Local runtime installed on 2026-05-16.

## Paths

- Hermes repo: `/Users/vc/codex/hermes-agent-latest`
- Hermes profile: `/Users/vc/.hermes/profiles/wechat`
- launchd plist: `/Users/vc/Library/LaunchAgents/ai.hermes.gateway-wechat.plist`
- logs: `/Users/vc/.hermes/profiles/wechat/logs/gateway.log`
- old Hermes backup: `/Users/vc/codex/hermes-reinstall-backup-20260516-124447`

## Model

- Provider: `openai-codex`
- Model: `gpt-5.5`
- Auth check:

```bash
HERMES_HOME=/Users/vc/.hermes/profiles/wechat hermes auth status openai-codex
```

## Weixin

- iLink bot account: `63ae9f0afdc6@im.bot`
- Allowed user: `o9cq804guoDz4lECFSwlnmtM3ggo@im.wechat`
- DM policy: `allowlist`
- Group policy: `disabled`

Credentials are stored in:

```text
/Users/vc/.hermes/profiles/wechat/.env
/Users/vc/.hermes/profiles/wechat/weixin/accounts/
```

Do not paste token values into chats or commits.

## Service Commands

```bash
HERMES_HOME=/Users/vc/.hermes/profiles/wechat hermes gateway status --deep
HERMES_HOME=/Users/vc/.hermes/profiles/wechat hermes gateway restart
HERMES_HOME=/Users/vc/.hermes/profiles/wechat hermes gateway stop
HERMES_HOME=/Users/vc/.hermes/profiles/wechat hermes gateway start
tail -f /Users/vc/.hermes/profiles/wechat/logs/gateway.log
```

launchd label:

```bash
launchctl print gui/501/ai.hermes.gateway-wechat
```

## Re-auth

Codex OAuth:

```bash
HERMES_HOME=/Users/vc/.hermes/profiles/wechat hermes auth add openai-codex
```

Weixin QR login:

```bash
HERMES_HOME=/Users/vc/.hermes/profiles/wechat /Users/vc/codex/hermes-agent-latest/venv/bin/python -c 'from hermes_cli.gateway import _setup_weixin; _setup_weixin()'
```

After a new Weixin pairing code is received:

```bash
HERMES_HOME=/Users/vc/.hermes/profiles/wechat hermes pairing list
HERMES_HOME=/Users/vc/.hermes/profiles/wechat hermes pairing approve weixin <CODE>
```

## Local Patch

`gateway/platforms/weixin.py` has a local fix so iLink `getupdates` HTTP 524 responses are treated as empty long-poll responses. Without this, the bot still works, but logs fill with `poll error ... HTTP 524`.

Verify after updates:

```bash
git -C /Users/vc/codex/hermes-agent-latest diff -- gateway/platforms/weixin.py
python3 -m py_compile /Users/vc/codex/hermes-agent-latest/gateway/platforms/weixin.py
HERMES_HOME=/Users/vc/.hermes/profiles/wechat hermes gateway restart
```
