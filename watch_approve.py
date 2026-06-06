#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
watch-approve: a PreToolUse hook that asks for permission on your Apple Watch.

Flow:
  stdin (JSON from the agent)
    -> POST a Pushcut notification (optionally through an HTTP proxy)
    -> you tap Allow / Deny on your iPhone / Apple Watch
    -> Pushcut runs a background web request that publishes allow/deny to an ntfy topic
    -> this script reads the result from the ntfy stream
    -> prints a permissionDecision back to the agent (Claude Code / Codex CLI)

Design principles:
  * Python 3 standard library only, no third-party dependencies.
  * All outbound requests can go through HTTPS_PROXY (e.g. a local Clash proxy).
  * Every value is read from environment variables; no secrets are hardcoded.
  * Any missing config / exception / timeout fails SAFE to "ask" (fall back to the
    agent's normal in-terminal approval prompt). The hook never crashes the agent.

Works with both Claude Code and Codex CLI: both send a JSON payload on stdin with
`tool_name` / `tool_input`, and both read back a `hookSpecificOutput.permissionDecision`
of allow / deny / ask.
"""

import json
import os
import re
import socket
import sys
import time
import urllib.error
import urllib.parse
import urllib.request


# ---------- Configuration: everything comes from environment variables ----------
PUSHCUT_KEY = os.environ.get("PUSHCUT_KEY", "").strip()
PUSHCUT_NOTIF = os.environ.get("PUSHCUT_NOTIF", "claude").strip() or "claude"
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "").strip()

# Label shown in the notification title, e.g. "Claude: Bash" / "Codex: apply_patch".
AGENT_LABEL = os.environ.get("WATCH_AGENT_LABEL", "Agent").strip() or "Agent"

# Proxy: prefer HTTPS_PROXY, then case/HTTP variants, e.g. http://127.0.0.1:7890
PROXY = (
    os.environ.get("HTTPS_PROXY")
    or os.environ.get("https_proxy")
    or os.environ.get("HTTP_PROXY")
    or os.environ.get("http_proxy")
    or ""
).strip()

# Total seconds to wait for the watch reply. Must be < the hook `timeout` in your
# agent config (we suggest 300 there, 240 here).
try:
    APPROVE_WAIT = int(float(os.environ.get("APPROVE_WAIT", "240")))
except ValueError:
    APPROVE_WAIT = 240

# What to decide when no reply arrives in time: allow / deny / ask (ask is safest).
TIMEOUT_DECISION = os.environ.get("APPROVE_TIMEOUT_DECISION", "ask").strip().lower()
if TIMEOUT_DECISION not in ("allow", "deny", "ask"):
    TIMEOUT_DECISION = "ask"

# Whether the hook injects the Allow/Deny buttons dynamically (needs Pushcut Pro).
# "1" (default): you only need an (empty) Pushcut notification named PUSHCUT_NOTIF;
#               the buttons and their ntfy URLs are added by this hook in the API call.
# "0": don't inject; use actions you configured by hand in the Pushcut app instead.
DYNAMIC_ACTIONS = os.environ.get("PUSHCUT_DYNAMIC_ACTIONS", "1").strip() != "0"

# Which Pushcut devices to target (names from GET /v1/devices), comma-separated.
# Empty = Pushcut default (all devices). Targeting the watch by name does NOT by itself
# beat Apple's routing (a normal alert still stays on the iPhone while it's in use) — the
# real lever for reliable watch delivery is PUSHCUT_TIME_SENSITIVE below.
PUSHCUT_DEVICES = [
    d.strip() for d in os.environ.get("PUSHCUT_DEVICES", "").split(",") if d.strip()
]

# Notification sound. Without a sound Pushcut delivers a SILENT notification and the
# watch/phone will NOT vibrate. Use "default" for a normal alert (watch vibrates),
# "vibrateOnly" to buzz without sound, "none" to send no sound. Other values per
# Pushcut: system / subtle / question / jobDone / problem / loud ...
PUSHCUT_SOUND = os.environ.get("PUSHCUT_SOUND", "default").strip()

# Mark the notification as Time-Sensitive. This is the single most effective lever to get
# it onto the Apple Watch even when the iPhone is unlocked/in use — a normal alert otherwise
# stays on the phone (Apple's routing). Time-Sensitive also breaks through Focus / Do Not
# Disturb. Default on; set PUSHCUT_TIME_SENSITIVE=0 to disable.
TIME_SENSITIVE = os.environ.get("PUSHCUT_TIME_SENSITIVE", "1").strip() != "0"

# Retries for triggering the Pushcut notification. Some networks/proxies occasionally
# drop the TLS handshake to api.pushcut.io (SSLEOFError); a few retries fix it (a
# failed handshake fails fast, so this does not usually add much delay).
try:
    PUSHCUT_RETRIES = max(1, int(os.environ.get("PUSHCUT_RETRIES", "12")))
except ValueError:
    PUSHCUT_RETRIES = 12

# Per-attempt timeout (seconds) for triggering Pushcut. Kept short so a stuck
# connection gives up quickly and the next retry can succeed.
try:
    PUSHCUT_TIMEOUT = max(3, int(os.environ.get("PUSHCUT_TIMEOUT", "6")))
except ValueError:
    PUSHCUT_TIMEOUT = 6

NTFY_BASE = os.environ.get("NTFY_BASE", "https://ntfy.sh/").strip() or "https://ntfy.sh/"
if not NTFY_BASE.endswith("/"):
    NTFY_BASE += "/"
# URL-escape the notification name so spaces / special chars don't break the URL.
PUSHCUT_URL = "https://api.pushcut.io/v1/notifications/" + urllib.parse.quote(
    PUSHCUT_NOTIF, safe=""
)

# Per-read socket timeout while reading the ntfy stream; must be > ntfy's keepalive
# (~45s) so a normal wait isn't mistaken for a dead connection. It's only a
# dead-connection fallback, not the total wait.
STREAM_READ_TIMEOUT = 60


# ---------- Danger filter: only escalate risky commands to the watch ----------
# When WATCH_DANGER_ONLY=1, anything that does NOT match a danger pattern returns
# "ask" immediately (defers to the agent's normal flow) WITHOUT buzzing your watch.
# This keeps routine commands quiet and only pings you for genuinely risky actions.
DANGER_ONLY = os.environ.get("WATCH_DANGER_ONLY", "0").strip() == "1"

# In danger-only mode, what to do with a NON-risky operation:
#   ask (default) = defer to the agent's normal flow (terminal may still prompt you)
#   allow         = auto-approve silently (no watch buzz, no terminal prompt) -> "only
#                   risky ops gate on the watch, everything else just runs"
#   deny          = block it
NONDANGER_DECISION = os.environ.get("WATCH_NONDANGER_DECISION", "ask").strip().lower()
if NONDANGER_DECISION not in ("allow", "deny", "ask"):
    NONDANGER_DECISION = "ask"

_DEFAULT_DANGER_PATTERNS = [
    r"\brm\s+-",                                       # rm with any flag (rm -rf, rm -r ...)
    r"\bsudo\b",
    r"\bgit\s+push\b.*(--force|-f|\s\+)",              # force push
    r"\bgit\s+reset\s+--hard\b",
    r"\bgit\s+clean\s+-[a-z]*f",
    r"\bdd\b.*\bif=",
    r"\bmkfs\b",
    r"\bof=/dev/",
    r">\s*/dev/sd",
    r"\b(shutdown|reboot|halt|poweroff)\b",
    r"\b(kill|pkill|killall)\b",
    r"\bchmod\s+(-r\s+|.*\b777\b)",
    r"\bchown\s+-r\b",
    r":\(\)\s*\{",                                     # fork bomb
    r"\b(drop|truncate)\s+(table|database)\b",
    r"\bdelete\s+from\b",
    r"(curl|wget)\b.*\|\s*(sudo\s+)?(sh|bash|zsh)\b",  # curl ... | sh
    r"\bnpm\s+publish\b",
    r"\bdocker\b.*\b(rm|prune|down)\b",
    r"\bterraform\s+destroy\b",
    r"\bkubectl\s+delete\b",
    r"\bremove-item\b.*-recurse.*-force",             # PowerShell
    r"\bdel\b\s+/[sf]",                                # cmd del /s /f
    r"\bformat\b\s+[a-z]:",                            # format C:
]
# WATCH_DANGER_REGEX fully replaces the defaults; WATCH_DANGER_EXTRA (newline-separated)
# adds to them.
_danger_override = os.environ.get("WATCH_DANGER_REGEX", "").strip()
_danger_extra = [p for p in os.environ.get("WATCH_DANGER_EXTRA", "").split("\n") if p.strip()]
_danger_sources = [_danger_override] if _danger_override else (_DEFAULT_DANGER_PATTERNS + _danger_extra)
_DANGER_RE = []
for _p in _danger_sources:
    try:
        _DANGER_RE.append(re.compile(_p, re.IGNORECASE))
    except re.error:
        pass


def is_dangerous(text):
    """Return True if `text` matches any danger pattern."""
    if not text:
        return False
    for rx in _DANGER_RE:
        if rx.search(text):
            return True
    return False


def emit(decision, reason):
    """Write the hook JSON to stdout and exit 0. decision in allow | deny | ask."""
    out = {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": decision,
            "permissionDecisionReason": reason,
        }
    }
    # Write UTF-8 bytes explicitly so a non-UTF-8 console code page (e.g. GBK on
    # Windows) can't garble the reason text the agent reads back.
    payload = json.dumps(out, ensure_ascii=False).encode("utf-8")
    try:
        sys.stdout.buffer.write(payload)
        sys.stdout.buffer.flush()
    except Exception:
        sys.stdout.write(json.dumps(out, ensure_ascii=False))
        sys.stdout.flush()
    sys.exit(0)


def make_opener():
    """Build an opener that explicitly uses the proxy; direct if none is set."""
    if PROXY:
        proxy_handler = urllib.request.ProxyHandler({"http": PROXY, "https": PROXY})
    else:
        proxy_handler = urllib.request.ProxyHandler({})  # ignore system proxy settings
    return urllib.request.build_opener(proxy_handler)


def describe(tool_name, tool_input):
    """Build a short human-readable description from tool_input."""
    ti = tool_input if isinstance(tool_input, dict) else {}
    if tool_name in ("Bash", "apply_patch"):
        desc = ti.get("command", "")  # Codex's apply_patch also carries `command`
    elif tool_name in ("Write", "Edit", "MultiEdit"):
        desc = ti.get("file_path", "")
    elif tool_name == "NotebookEdit":
        desc = ti.get("notebook_path", "")
    else:
        desc = ""
    if not desc:
        try:
            desc = json.dumps(ti, ensure_ascii=False)
        except Exception:
            desc = str(ti)
    desc = " ".join(str(desc).split())  # flatten whitespace/newlines
    if len(desc) > 300:
        desc = desc[:297] + "..."
    return desc


def build_actions():
    """Build Allow/Deny buttons as BACKGROUND web requests that GET-publish to ntfy.

    Important: use urlBackgroundOptions (a background web request), NOT a plain `url`
    (which means "open URL / open app"). watchOS does not support "open app / run
    shortcut" actions, only background web requests. Using GET (ntfy supports
    /publish?message=xxx) avoids the occasionally-flaky httpBody in urlBackgroundOptions.
    Returns None to inject nothing (use app-configured actions instead).
    """
    if not DYNAMIC_ACTIONS or not NTFY_TOPIC:
        return None
    base = NTFY_BASE + urllib.parse.quote(NTFY_TOPIC, safe="") + "/publish?message="
    return [
        {"name": "Allow", "url": base + "allow", "urlBackgroundOptions": {"httpMethod": "GET"}},
        {"name": "Deny", "url": base + "deny", "urlBackgroundOptions": {"httpMethod": "GET"}},
    ]


def send_pushcut(opener, title, text):
    """Trigger the Pushcut notification; retry transient network/TLS failures.

    A 4xx (e.g. 404 notification-not-found, 401 bad key) is a config error that retries
    can't fix, so it's raised immediately -> the caller fails safe to "ask".
    """
    payload = {"title": title, "text": text}
    actions = build_actions()
    if actions:
        payload["actions"] = actions
    if PUSHCUT_DEVICES:
        payload["devices"] = PUSHCUT_DEVICES
    if PUSHCUT_SOUND and PUSHCUT_SOUND.lower() != "none":
        payload["sound"] = PUSHCUT_SOUND
    if TIME_SENSITIVE:
        payload["isTimeSensitive"] = True
    body = json.dumps(payload).encode("utf-8")

    last = None
    for attempt in range(PUSHCUT_RETRIES):
        try:
            req = urllib.request.Request(
                PUSHCUT_URL,
                data=body,
                method="POST",
                headers={"API-Key": PUSHCUT_KEY, "Content-Type": "application/json"},
            )
            with opener.open(req, timeout=PUSHCUT_TIMEOUT) as resp:
                resp.read()
            return
        except urllib.error.HTTPError as e:
            # 4xx (except 429 rate-limit) is a config error; don't bother retrying.
            if 400 <= e.code < 500 and e.code != 429:
                raise
            last = e
        except Exception as e:
            last = e
        if attempt < PUSHCUT_RETRIES - 1:
            time.sleep(0.3)
    if last is not None:
        raise last


def wait_for_decision(opener, since_ts, deadline):
    """Read allow/deny from the ntfy stream. Returns 'allow'/'deny', or None at deadline.

    Using since=since_ts (the t0 captured BEFORE sending the notification) avoids a race:
    even if you tap instantly and the reply lands before we subscribe, ntfy replays
    messages since t0 on (re)connect, so nothing is missed.
    """
    url = NTFY_BASE + urllib.parse.quote(NTFY_TOPIC, safe="") + "/json?since=" + str(since_ts)
    while time.monotonic() < deadline:
        try:
            resp = opener.open(url, timeout=STREAM_READ_TIMEOUT)
        except Exception:
            time.sleep(1)  # open failed; reconnect shortly (bounded by deadline)
            continue
        try:
            while time.monotonic() < deadline:
                try:
                    raw = resp.readline()
                except socket.timeout:
                    break  # connection went quiet too long; reconnect
                except Exception:
                    break
                if not raw:
                    break  # server closed the connection; reconnect
                line = raw.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line.decode("utf-8", "replace"))
                except Exception:
                    continue
                if obj.get("event") != "message":
                    continue  # only messages; ignore open / keepalive
                msg = (obj.get("message") or "").strip().lower()
                if msg in ("allow", "approve", "yes", "ok"):
                    return "allow"
                if msg in ("deny", "block", "no"):
                    return "deny"
                # anything else: ignore and keep waiting
        finally:
            try:
                resp.close()
            except Exception:
                pass
    return None


def main():
    # 1) Read stdin JSON; on parse failure fall back to "ask".
    #    Read bytes + decode utf-8-sig to also strip a possible BOM (some shells/pipes
    #    add one), then strip surrounding whitespace for maximum compatibility.
    try:
        raw_bytes = sys.stdin.buffer.read()
        raw = raw_bytes.decode("utf-8-sig", "replace").strip()
        data = json.loads(raw) if raw else {}
    except Exception:
        emit("ask", "watch-approve: could not parse hook input; falling back to normal approval.")
    if not isinstance(data, dict):
        data = {}

    tool_name = data.get("tool_name", "Tool")
    tool_input = data.get("tool_input", {})

    # 2) Missing critical config -> fall back to "ask" (don't error out).
    if not PUSHCUT_KEY or not NTFY_TOPIC:
        emit("ask", "watch-approve: PUSHCUT_KEY or NTFY_TOPIC missing; falling back to normal approval.")

    desc = describe(tool_name, tool_input)

    # In danger-only mode, handle non-risky operations per NONDANGER_DECISION (no watch).
    if DANGER_ONLY:
        match_text = ""
        if isinstance(tool_input, dict):
            match_text = str(tool_input.get("command") or tool_input.get("file_path") or "")
        if not is_dangerous(match_text or desc):
            if NONDANGER_DECISION == "allow":
                emit("allow", "watch-approve: not risky; auto-approved (no watch).")
            elif NONDANGER_DECISION == "deny":
                emit("deny", "watch-approve: not risky; denied per config.")
            else:
                emit("ask", "watch-approve: not flagged as risky; deferring to normal approval.")

    opener = make_opener()

    # 3) Race-safe: record t0, then send the notification, then subscribe with since=t0.
    t0 = int(time.time())
    deadline = time.monotonic() + APPROVE_WAIT

    title = AGENT_LABEL + ": " + str(tool_name)
    text = desc if desc else "(no details)"
    try:
        send_pushcut(opener, title, text)
    except urllib.error.HTTPError as e:
        hint = ""
        if e.code == 404:
            hint = " (no notification named '%s'? create one in the Pushcut app)" % PUSHCUT_NOTIF
        elif e.code in (401, 403):
            hint = " (invalid PUSHCUT_KEY?)"
        emit("ask", "watch-approve: Pushcut returned HTTP %s%s; falling back to normal approval." % (e.code, hint))
    except Exception as e:
        emit(
            "ask",
            "watch-approve: failed to reach Pushcut (%s, likely proxy/network); falling back to normal approval."
            % type(e).__name__,
        )

    # 4) Wait for the watch reply.
    try:
        decision = wait_for_decision(opener, t0, deadline)
    except Exception:
        decision = None

    if decision == "allow":
        emit("allow", "watch-approve: approved on watch.")
    elif decision == "deny":
        emit("deny", "watch-approve: denied on watch.")
    else:
        emit(
            TIMEOUT_DECISION,
            "watch-approve: no reply within %ss; applying timeout policy '%s'."
            % (APPROVE_WAIT, TIMEOUT_DECISION),
        )


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as e:
        # Last resort: any unexpected error fails safe to "ask"; never crash.
        try:
            emit("ask", "watch-approve: unexpected error (%s); falling back to normal approval." % type(e).__name__)
        except Exception:
            sys.exit(0)
