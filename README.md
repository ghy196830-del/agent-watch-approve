# watch-approve

Approve your coding agent's actions from your **Apple Watch**.

When **Claude Code** or **Codex CLI** is about to run a shell command or edit a file, this
hook pushes a notification to your iPhone / Apple Watch. You tap **Allow** or **Deny** on
your wrist, and the decision flows back to gate the action — no need to look at the terminal.

> 中文文档见 [README.zh-CN.md](./README.zh-CN.md)

```
PreToolUse hook
  → POST a notification to Pushcut (with Allow/Deny background-request buttons)
  → iPhone (locked) mirrors it to the Apple Watch; you tap Allow / Deny
  → Pushcut runs the button's background web request → publishes allow/deny to an ntfy topic
  → the hook reads the result from the ntfy stream
  → returns permissionDecision (allow / deny / ask) to the agent
```

It's a single dependency-free Python file (`watch_approve.py`, standard library only), and it
**fails safe**: any missing config, network error, or timeout falls back to the agent's normal
in-terminal approval prompt instead of crashing or blocking forever.

---

## Why

- You're waiting on a long agent run and you're on your phone / away from the keyboard.
- You want a human gate on dangerous actions (`rm -rf`, `git push --force`, prod deploys) without
  babysitting the terminal.
- It works from behind a proxy (designed/tested with a local Clash HTTP proxy), so it's usable in
  networks where direct access to Pushcut/ntfy is unreliable.

## How it works

The round-trip uses two cheap, scriptable services:

- **[Pushcut](https://www.pushcut.io/)** — sends the push notification with action buttons. Dynamic
  buttons (injected by this hook) need **Pushcut Pro**.
- **[ntfy](https://ntfy.sh/)** — a pub/sub topic used as the return channel. The public `ntfy.sh`
  server works; the topic name acts as the password, so make it long and random.

The button is a **background web request** (not "open URL"), because watchOS only supports
background web requests in notification actions — not "open app" / "run shortcut" actions. It
GET-publishes to `https://ntfy.sh/<topic>/publish?message=allow` (or `deny`).

## Requirements

- **Claude Code** and/or **Codex CLI** (both support a `PreToolUse` hook with the same contract).
- **Python 3** on PATH (standard library only — nothing to `pip install`).
- A **Pushcut** account (**Pro** for dynamically-injected buttons).
- The **Pushcut app installed on the Apple Watch** (so the watch can receive + act).
- An **ntfy** topic (public `ntfy.sh` is fine).

---

## Setup

### 1. Pushcut
1. Create an account, install the app on iPhone **and Apple Watch**.
2. Create a Notification (the name goes in `PUSHCUT_NOTIF`, e.g. `claude`). Leave title/text empty —
   the hook overrides them. With dynamic actions (default) you do **not** need to add buttons by hand.
3. Get your API key from **Account → API**.

### 2. ntfy
Pick a long random topic name, e.g. `myagent_8f3k2j9x`. Nothing to install — just choose it.

### 3. Install the hook
Put `watch_approve.py` somewhere stable and point your agent at it.

**Claude Code** — merge [`examples/claude/settings.example.json`](./examples/claude/settings.example.json)
into `.claude/settings.json` (project) or `~/.claude/settings.json` (global). Set the env values and
the absolute path to the script.

**Codex CLI** — put [`examples/codex/hooks.example.json`](./examples/codex/hooks.example.json) at
`~/.codex/hooks.json` (or `<repo>/.codex/hooks.json`). Codex doesn't inject env vars per hook, so set
`PUSHCUT_KEY` / `NTFY_TOPIC` / `HTTPS_PROXY` / `PUSHCUT_SOUND` etc. as **system/user environment
variables**. Then trust the hook with the `/hooks` command (or run `codex --dangerously-bypass-hook-trust`
once for a one-off).

> Codex's `PreToolUse` covers `Bash`, `apply_patch` (file edits), and MCP tools. Claude Code's covers
> `Bash`, `Write`, `Edit`, `MultiEdit`, etc. The matcher in each example reflects this.

### 4. Test it (without the agent)
```bash
# set env first (see table below), then:
echo '{"hook_event_name":"PreToolUse","tool_name":"Bash","tool_input":{"command":"echo hello"}}' \
  | python /path/to/watch_approve.py
```
Lock your iPhone so the notification reaches the watch, tap **Allow**, and you should immediately get:
```json
{"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"allow","permissionDecisionReason":"watch-approve: approved on watch."}}
```

---

## Configuration (environment variables)

| Variable | Default | Description |
|----------|---------|-------------|
| `PUSHCUT_KEY` | — | **Required.** Pushcut API key. |
| `NTFY_TOPIC` | — | **Required.** Your secret ntfy topic (acts as a password — make it random). |
| `PUSHCUT_NOTIF` | `claude` | Name of the Pushcut notification to trigger. |
| `HTTPS_PROXY` | — | Proxy for all outbound requests, e.g. `http://127.0.0.1:7890`. Falls back to `HTTP_PROXY`. |
| `PUSHCUT_SOUND` | `default` | Notification sound. **Without one the watch won't vibrate.** `vibrateOnly` = buzz, no sound; `none` = silent. |
| `WATCH_AGENT_LABEL` | `Agent` | Title prefix, e.g. `Claude` → "Claude: Bash". |
| `APPROVE_WAIT` | `240` | Seconds to wait for the reply. Keep it **below** the hook `timeout` (300). |
| `APPROVE_TIMEOUT_DECISION` | `ask` | Decision if nobody replies in time: `ask` / `allow` / `deny`. |
| `PUSHCUT_DYNAMIC_ACTIONS` | `1` | `1` = hook injects Allow/Deny buttons (Pushcut Pro). `0` = use app-configured actions. |
| `PUSHCUT_DEVICES` | — | Comma-separated device names to target (see `GET /v1/devices`). Empty = all. |
| `PUSHCUT_RETRIES` | `12` | Retry count for triggering Pushcut (handles flaky TLS to api.pushcut.io). |
| `PUSHCUT_TIMEOUT` | `6` | Per-attempt timeout (s) for triggering Pushcut. |
| `NTFY_BASE` | `https://ntfy.sh/` | ntfy server base URL (change if you self-host). |

---

## Apple Watch notes (important)

- **The watch only shows the notification when the iPhone is locked/asleep.** If the phone is
  unlocked/in use, iOS keeps the alert on the phone. This is Apple's behavior, not a bug — and it fits
  the use case (phone locked on the desk while you code → it goes to the watch; you're on your phone →
  the phone shows it). Either way you're notified on the device you're using.
- **Install the Pushcut app on the watch.** Without it the watch can't act on the notification.
- **Buttons must be background web requests** (this hook does that). watchOS rejects "open app / run
  shortcut" actions with *"actions that run shortcuts or open apps are not supported on watchOS"*.
- **Set a sound** (`PUSHCUT_SOUND=default`) or the watch won't vibrate.

## Latency

There's an inherent floor of a few seconds: the notification trigger round-trips to Pushcut, and the
reply round-trips back through ntfy, plus Apple's watch delivery. If you're behind a slow/unstable
proxy, picking a faster proxy node for `api.pushcut.io` and `ntfy.sh` helps the most.

## Troubleshooting

Read `permissionDecisionReason` in the output — it says what happened:

| Symptom | Cause / fix |
|---------|-------------|
| `Pushcut returned HTTP 404` | No notification with that `PUSHCUT_NOTIF` name exists in Pushcut's cloud (create it, and make sure the app synced it). |
| `Pushcut returned HTTP 401/403` | Wrong `PUSHCUT_KEY`. |
| `failed to reach Pushcut (...)` | Proxy/network down, or repeated TLS failures (raise `PUSHCUT_RETRIES`). |
| Phone buzzes, watch doesn't | iPhone wasn't locked, or Pushcut app not installed on the watch. |
| Watch shows it but tapping says "not supported" | The action isn't a background web request (default config already is). |
| No vibration | Set `PUSHCUT_SOUND=default` (or `vibrateOnly`). |

Any failure path returns `ask`, so the agent just falls back to its normal prompt — it never hangs.

## Security

- Secrets (`PUSHCUT_KEY`, `NTFY_TOPIC`) are read from env vars and never hardcoded. Don't commit a
  real `settings.json` — `.gitignore` excludes it.
- The ntfy topic name is the only thing guarding your return channel on public `ntfy.sh`. Use a long
  random value, or self-host ntfy with auth (`NTFY_BASE`).
- `allow` makes the agent skip its own permission prompt. Scope the `matcher` to the tools you actually
  want gated.

## License

MIT — see [LICENSE](./LICENSE).
