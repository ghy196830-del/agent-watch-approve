# watch-approve

Approve your coding agent's actions from your **Apple Watch**.

When **Claude Code** or **Codex CLI** is about to run a shell command or edit a file, this
hook pushes a notification to your iPhone / Apple Watch. You tap **Allow** or **Deny** on
your wrist, and the decision flows back to gate the action — no need to look at the terminal.

> 中文文档见 [README.zh-CN.md](./README.zh-CN.md)

```
PreToolUse hook
  → POST a notification to Pushcut (with Allow/Deny background-request buttons)
  → it's sent Time-Sensitive, so it reaches the Apple Watch even while the iPhone is in use; you tap Allow / Deny
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
Tap **Allow** (the alert is Time-Sensitive by default, so it reaches the watch even if the iPhone is unlocked), and you should immediately get:
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
| `PUSHCUT_TIME_SENSITIVE` | `1` | `1` = mark the alert **Time-Sensitive** → reaches the **Apple Watch even while the iPhone is in use**, and breaks through Focus/DND. `0` = normal alert (watch only when the phone is locked). |
| `WATCH_AGENT_LABEL` | `Agent` | Title prefix, e.g. `Claude` → "Claude: Bash". |
| `APPROVE_WAIT` | `240` | Seconds to wait for the reply. Keep it **below** the hook `timeout` (300). |
| `APPROVE_TIMEOUT_DECISION` | `ask` | Decision if nobody replies in time: `ask` / `allow` / `deny`. |
| `PUSHCUT_DYNAMIC_ACTIONS` | `1` | `1` = hook injects Allow/Deny buttons (Pushcut Pro). `0` = use app-configured actions. |
| `PUSHCUT_DEVICES` | — | Comma-separated device names to target (see `GET /v1/devices`). Empty = all. |
| `PUSHCUT_RETRIES` | `12` | Retry count for triggering Pushcut (handles flaky TLS to api.pushcut.io). |
| `PUSHCUT_TIMEOUT` | `6` | Per-attempt timeout (s) for triggering Pushcut. |
| `NTFY_BASE` | `https://ntfy.sh/` | ntfy server base URL (change if you self-host). |
| `WATCH_DANGER_ONLY` | `0` | `1` = only "risky" commands ping the watch; everything else returns `ask` instantly (see below). |
| `WATCH_DANGER_EXTRA` | — | Extra danger regexes to add (newline-separated), case-insensitive. |
| `WATCH_DANGER_REGEX` | — | A single regex that **replaces** the built-in danger list entirely. |
| `WATCH_NONDANGER_DECISION` | `ask` | (danger-only mode) what to do with non-risky ops: `ask` (defer to the terminal), `allow` (auto-run silently — no watch, no prompt), `deny`. |

---

## Reducing noise (danger-only mode)

By default the hook asks for approval on **every** tool call matched by `matcher` — that can be a lot.
Set `WATCH_DANGER_ONLY=1` and the hook only pings your watch for **risky** commands (the rest return
`ask` immediately, so the agent behaves normally and your watch stays quiet).

Recommended low-noise setup: `WATCH_DANGER_ONLY=1` plus a narrow `"matcher": "Bash"`. (A narrow matcher
stays quiet, but tools it doesn't list fall back to the terminal — see **Auto-pilot** below for a fully
terminal-free variant.)

The built-in danger list flags things like `rm -rf`, `sudo`, `git push --force`, `git reset --hard`,
`dd`, `mkfs`, `chmod 777`, `shutdown`/`reboot`, `kill`, `drop/truncate table`, `delete from`,
`curl ... | sh`, `docker prune`, `terraform destroy`, `kubectl delete`, PowerShell `Remove-Item -Recurse
-Force`, etc. Extend it with `WATCH_DANGER_EXTRA`, or replace it wholesale with `WATCH_DANGER_REGEX`.

> If you enable danger-only, test with a risky command (e.g. `rm -rf /tmp/x`) — a plain `echo hello`
> won't trigger a notification by design.

**Auto-pilot with a safety net.** By default, danger-only sends non-risky ops to `ask` (so the terminal
may still prompt). If you'd rather *never* touch the terminal — "only risky ops gate on my watch,
everything else just runs" — combine **`"matcher": "*"`** with `WATCH_DANGER_ONLY=1` and
`WATCH_NONDANGER_DECISION=allow`. Then *every* tool routes through the hook: non-risky ones (reads, web
search, MCP calls, …) are auto-approved silently, and only danger-list matches buzz your watch.

> **Why `"matcher": "*"` and not a narrow one?** A matcher is a whitelist — any tool it *doesn't* list
> (`WebSearch`, MCP tools, tomorrow's new tool, …) skips the hook entirely and falls back to the agent's
> **in-terminal** prompt, which never reaches your watch or phone. A whitelist always misses the next new
> tool, so for a no-terminal setup, match **all** tools and let the danger filter decide. (Claude Code
> docs: `"*"`, `""`, or an omitted matcher all mean "match every tool".)

> ⚠️ Two things to keep in mind: (1) Only pair `"matcher": "*"` with `WATCH_DANGER_ONLY=1` — a `"*"`
> matcher *without* danger-only would buzz your watch on **every** tool call (including every file read).
> (2) With `WATCH_NONDANGER_DECISION=allow`, anything the danger list does **not** catch runs without any
> confirmation. The danger regex is your only gate — review/extend it (`WATCH_DANGER_EXTRA`) to taste.

---

## Apple Watch notes (important)

- **Time-Sensitive is what gets it to the watch reliably.** By default a *normal* notification only
  reaches the Apple Watch when the iPhone is **locked/asleep**; while the phone is unlocked/in use, iOS
  keeps the alert on the phone (Apple's routing, not a bug). Marking it **Time-Sensitive**
  (`PUSHCUT_TIME_SENSITIVE=1`, the default) breaks through that, so it reaches the watch **even while
  you're using the iPhone** — and through Focus / Do Not Disturb. A/B-tested on-device: two
  otherwise-identical notifications, only the Time-Sensitive one reached the watch while the phone was in
  use. (Needs Time-Sensitive allowed for Pushcut under iOS Settings → Notifications — the default.)
- **Install the Pushcut app on the watch.** Without it the watch can't act on the notification.
- **Buttons must be background web requests** (this hook does that). watchOS rejects "open app / run
  shortcut" actions with *"actions that run shortcuts or open apps are not supported on watchOS"*.
- **Set a sound** (`PUSHCUT_SOUND=default`) or the watch won't vibrate.
- **If a device suddenly stops receiving, reopen the Pushcut app on it.** The app's push token can go stale after it's been killed/backgrounded for a while — Pushcut keeps reporting "success" while nothing arrives. Reopening re-registers the token (and re-syncs the watch).

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
| Hook sends with no error, but **no device** receives the notification | Pushcut's API accepted the push (returns success) but APNs didn't deliver it — most often a **stale push token** because the Pushcut app was killed / backgrounded too long. **Open the Pushcut app on the iPhone** to re-register its token (this also re-syncs the watch). Remember: a Pushcut "success" only means the cloud *accepted* the push, not that a device *received* it. |
| Phone buzzes, watch doesn't | Keep `PUSHCUT_TIME_SENSITIVE=1` (default) so the alert is Time-Sensitive and reaches the watch while the phone is in use. Also check the Pushcut app is installed on the watch and that Time-Sensitive is allowed for Pushcut in iOS Settings → Notifications. |
| Agent still prompts in the **terminal** (never reaches the watch/phone) | That tool isn't covered by your `matcher` (a whitelist) — e.g. `WebSearch` or MCP tools. Use `"matcher": "*"` together with `WATCH_DANGER_ONLY=1` + `WATCH_NONDANGER_DECISION=allow` so every tool routes through the hook. |
| Watch shows it but tapping says "not supported" | The action isn't a background web request (default config already is). |
| No vibration | Set `PUSHCUT_SOUND=default` (or `vibrateOnly`). |
| Notification text shows as `???` (Windows manual test) | A PowerShell artifact, not the hook: piping non-ASCII into a native program re-encodes it as `?`, because `$OutputEncoding` defaults to ASCII. The hook itself reads UTF-8 and sends `\uXXXX`-escaped JSON, which renders correctly when the agent runs it. To test non-ASCII on Windows, feed the JSON from a UTF-8 file or via environment variables instead of an inline pipe. |

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
