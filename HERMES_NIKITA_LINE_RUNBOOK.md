# Hermes Nikita LINE Runbook

Last updated: 2026-05-16

Nikita is now running on Hermes Agent, not the old Nixie `codex_line_proxy.py`.

## Live Contract

- Hermes profile: `/Users/vc/.hermes/profiles/nikita`
- Config: `/Users/vc/.hermes/profiles/nikita/config.yaml`
- Secrets: `/Users/vc/.hermes/profiles/nikita/.env`
- Persona: `/Users/vc/.hermes/profiles/nikita/SOUL.md`
- Memory: `/Users/vc/.hermes/profiles/nikita/memories/MEMORY.md`
- LaunchAgent: `ai.hermes.gateway-nikita`
- Plist: `/Users/vc/Library/LaunchAgents/ai.hermes.gateway-nikita.plist`
- Local LINE port: `18795`
- Public health: `https://nikita.shinningsuperman.com/line/webhook/health`
- LINE webhook: `https://nikita.shinningsuperman.com/line/webhook`
- Model provider: `openai-codex`
- Model: `gpt-5.5`

The old Nixie runtime is still present at:

```text
/Users/vc/Library/Application Support/nikita-line-agent/runtime
```

Its LaunchAgent, `com.vc.nikita-codex-line-proxy`, should remain unloaded while
Hermes is live.

## Service Commands

```bash
/Users/vc/codex/hermes-agent-latest/venv/bin/python -m hermes_cli.main --profile nikita gateway status --deep
/Users/vc/codex/hermes-agent-latest/venv/bin/python -m hermes_cli.main --profile nikita gateway restart
/Users/vc/codex/hermes-agent-latest/venv/bin/python -m hermes_cli.main --profile nikita gateway stop
/Users/vc/codex/hermes-agent-latest/venv/bin/python -m hermes_cli.main --profile nikita gateway start
```

## Validation

```bash
curl -s http://127.0.0.1:18795/line/webhook/health
curl -s https://nikita.shinningsuperman.com/line/webhook/health
launchctl print gui/$(id -u)/ai.hermes.gateway-nikita
```

LINE Developers webhook endpoint should be:

```text
https://nikita.shinningsuperman.com/line/webhook
```

## Rollback To Nixie Proxy

Only do this intentionally:

1. Stop Hermes Nikita:

   ```bash
   /Users/vc/codex/hermes-agent-latest/venv/bin/python -m hermes_cli.main --profile nikita gateway stop
   ```

2. Load the old Nixie proxy:

   ```bash
   launchctl bootstrap gui/$(id -u) /Users/vc/Library/LaunchAgents/com.vc.nikita-codex-line-proxy.plist
   launchctl kickstart -k gui/$(id -u)/com.vc.nikita-codex-line-proxy
   ```

3. Change LINE Developers webhook endpoint back to:

   ```text
   https://nikita.shinningsuperman.com/webhook
   ```
