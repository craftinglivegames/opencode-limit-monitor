# OpenCode Limit Monitor

A background monitor + GUI for [OpenCode](https://opencode.ai) that keeps an eye
on your **free models**, **sessions**, and **network**, with auto-recovery and
auto-backup. Built for people running OpenCode with free model tiers that hit
rate limits often.

![Python](https://img.shields.io/badge/python-3.x-blue) ![License](https://img.shields.io/badge/license-MIT-green)

## What it does

1. **Continuously probes free models** in the background and shows live status:
   `WORKS` / `LIMIT` / `DEAD` / `waiting`, plus context size, provider, and last
   check time.
2. **Shows all open sessions** with the model each uses, outcome, how long it has
   been idle, and **context usage %** (the same "Usage" figure OpenCode shows in
   the top-right corner — computed from the last completed message's tokens
   divided by the model's context limit).
3. **Auto-recovery of interrupted sessions** (toggleable globally, plus
   per-session checkboxes):
   - wakes a stalled/failed session with a recovery phrase (default `continue`);
   - first tries the **same model** if its limit has reset;
   - otherwise falls back to the first working model from the router order;
   - if nothing is free, waits and wakes as soon as one frees up.
4. **Per-session checkboxes**: exclude a session from recovery, disable routing
   for it (pin the current model so it is never switched), exclude it from
   backup, or mark it "do not touch".
5. **Auto-backup of sessions**: after every AI response the session and its
   messages are exported to JSON in a folder you choose (one subfolder per
   session, old backup overwritten). Per-session exclusion checkbox included.
6. **WARP / oplire buttons**: status / connect / disconnect / reset (new IP) /
   quick-reset / download oplire — no terminal typing. Component checks.
7. **HTTP interface** for other tools / AIs (see below).

## Install

Double-click **`install.bat`**. It finds Python, copies the app to
`%LOCALAPPDATA%\OpenCodeLimitMonitor`, creates a Desktop shortcut and (optional)
an autostart entry.

Or run from anywhere: `python opencode_limit_monitor.py`

Add `--headless` to run without a window (for a service / scheduled task).

## Tabs

- **Models** — live free-model table. Buttons: *Probe all models*, *Reset limits
  (WARP/oplire)*.
- **Sessions** — session table + a panel for the selected session with the
  checkboxes (do-not-touch / exclude from recovery / no routing / exclude from
  backup) and buttons (pin current model, unpin, recover now, backup now).
- **Network & WARP** — WARP status, public IP, component checks, WARP/oplire
  buttons.
- **Settings** — recovery phrase, global auto-recovery toggle, auto-backup
  toggle + backup folder picker, router model order.
- **Journal** — live log.

## HTTP interface

`http://127.0.0.1:8799`

- `GET /state` — everything: models, sessions (with usage %), network, components, log.
- `GET /models` · `GET /sessions` · `GET /log`
- `POST /command` with a JSON body:
  ```json
  {"action": "reset_ip"}                       // rotate IP via WARP/oplire
  {"action": "probe_all"}                      // re-probe all free models
  {"action": "wake",          "params": {"sid": "ses_..."}}
  {"action": "switch_model",  "params": {"sid": "ses_...", "model": "glm-5.3-flash-free"}}
  {"action": "set_flag",      "params": {"sid": "ses_...", "flag": "recover_off", "value": true}}
  {"action": "pin_model",     "params": {"sid": "ses_...", "model": "mimo-v2.6-flash-free"}}
  {"action": "backup_now",    "params": {"sid": "ses_..."}}       // omit sid = backup all
  {"action": "set_backup_root",    "params": {"path": "D:\\Backups"}}
  {"action": "set_backup_enabled", "params": {"value": true}}
  {"action": "set_phrase",        "params": {"phrase": "продолжай"}}
  {"action": "set_auto_recover",  "params": {"value": true}}
  {"action": "set_prefer",        "params": {"prefer": ["glm-5.3-flash-free", "..."]}}
  ```
  Flags for `set_flag`: `no_touch`, `recover_off`, `no_route`, `backup_off`.

## Config

Settings are stored in `limit_monitor_config.json` next to the script; the log
is `limit_monitor.log`. All paths (opencode-cli, warp-cli, oplire) are
auto-detected.

## Notes

- Free-model probes reuse one throwaway session and cost a single tiny message
  per model (they are free tiers).
- Session backups read at most the **200 most recent messages** per session (the
  API caps `limit` at 200) — enough for crash protection.
- Auto-recovery only touches sessions that are idle past a threshold and marked
  as failed/interrupted; sessions you excluded or marked "do not touch" are
  never touched.

## License

MIT — see [LICENSE](LICENSE).
