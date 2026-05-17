# Daisy Hermes Stability

Small local deployment assets used by the Daisy Hermes LINE profile.

## Session Guard

`session_guard.py` rotates overgrown LINE sessions before a profile starts
carrying very large short-term chat history into every turn. It preserves
existing transcript files for search/resume and writes a fresh active
`sessions.json` entry, so long-term profile memory is not deleted.

Default thresholds:

- `DAISY_SESSION_GUARD_MAX_PROMPT_TOKENS=60000`
- `DAISY_SESSION_GUARD_MAX_TRANSCRIPT_BYTES=2000000`
- `DAISY_SESSION_GUARD_MAX_JSONL_BYTES=2000000`
- `DAISY_SESSION_GUARD_MIN_AGE_SECONDS=180`

Install on macOS by copying the plist example into
`~/Library/LaunchAgents/com.openclaw.daisy-session-guard.plist`, adjusting
absolute paths if needed, then loading it:

```bash
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.openclaw.daisy-session-guard.plist
```

Validate:

```bash
python3 contrib/daisy/session_guard.py --dry-run
launchctl print gui/$(id -u)/com.openclaw.daisy-session-guard
```
