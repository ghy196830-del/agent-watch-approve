#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Claude Code / Codex CLI Stop / StopFailure hook: 任务完成 + 限额提醒。

三种通知(都是纯提醒:没有按钮、不需回复,看一眼即可):
  * Stop(正常完成)          ->「🦀/🤖 任务已完成」
  * Stop 且 Codex 额度过阈值 ->「⚠️ Codex 额度已用 NN%」(订阅额度快烧完的预警;
      数据来自 Codex rollout 里的 rate_limits 遥测,见下方 read_codex_rate)
  * StopFailure(Claude 专属:回合因 API 错误终止时**代替 Stop** 触发)
      - error=rate_limit / 文案命中限额 ->「🚦 Claude 额度已用完」+ 重置时间
      - 其它 API 错误                   ->「⚠️ Claude 任务异常终止」+ 错误类型
    (Codex 没有 StopFailure:它限额报错的回合直接 break、不跑任何 Stop hook,
     所以 Codex 端靠上面的「过阈值预警」在烧完之前提醒你。)

设计原则(与 watch_approve.py 一致):
  * 只用 Python 3 标准库,不引第三方依赖。
  * 所有出网请求显式走 HTTPS_PROXY(机场本地代理)。
  * 配置全部从环境变量读(支持同目录 watch.env 兜底),绝不硬编码密钥。
  * fire-and-forget:任何配置缺失 / 异常 / 超时,一律静默 exit 0,
    既不阻塞 agent,也绝不触发 Stop 循环(永不输出 decision=block)。
  * 多窗口并行:正文末尾带「📁 项目文件夹名」,分清是哪个窗口完成了任务。
"""

import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request


# ---------- 兜底配置文件 watch.env ----------
# hook 进程的环境变量取决于「谁启动了 agent」:Claude Code 会把 settings.json 的 env
# 注入自己进程(hook 继承得到);但 Codex 不会把 config.toml 里 shell_environment_policy
# 的 env 传给 hook(那只作用于 shell 工具,见 codex-rs/hooks/engine/command_runner.rs)。
# 为了让脚本在任何宿主下都拿得到配置,这里读脚本同目录的 watch.env(KEY=VALUE 每行一条,
# # 开头是注释),【只填补缺失的环境变量】——真实环境变量永远优先。路径可用 WATCH_ENV_FILE 覆盖。
def _load_env_file():
    path = os.environ.get("WATCH_ENV_FILE", "").strip() or os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "watch.env"
    )
    try:
        with open(path, "r", encoding="utf-8-sig") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                k, v = k.strip(), v.strip()
                if k and k not in os.environ:
                    os.environ[k] = v
    except Exception:
        pass


_load_env_file()


# ---------- 配置:全部来自环境变量(与审批 hook 共用同一套) ----------
PUSHCUT_KEY = os.environ.get("PUSHCUT_KEY", "").strip()
PUSHCUT_NOTIF = os.environ.get("PUSHCUT_NOTIF", "claude").strip() or "claude"

# 代理:优先 HTTPS_PROXY,其次大小写/HTTP 变体,形如 http://127.0.0.1:7890
PROXY = (
    os.environ.get("HTTPS_PROXY")
    or os.environ.get("https_proxy")
    or os.environ.get("HTTP_PROXY")
    or os.environ.get("http_proxy")
    or ""
).strip()

# 指定通知发给哪些 Pushcut 设备(设备名见 GET /v1/devices)。逗号分隔,例如 "iPhone,watch"。
# 留空 = 用 Pushcut 默认(发给所有设备)。
PUSHCUT_DEVICES = [
    d.strip() for d in os.environ.get("PUSHCUT_DEVICES", "").split(",") if d.strip()
]

# 完成提醒的声音。Pushcut 不带 sound 会被当成静默通知,手表/手机不震。
# 默认用 "jobDone"——Pushcut 内置的「任务完成」提示音,语义正好;设 "none" 则不带。
DONE_SOUND = os.environ.get(
    "WATCH_DONE_SOUND", os.environ.get("PUSHCUT_SOUND", "jobDone")
).strip()

# 限额类通知(额度用完 / 接近限额 / 异常终止)的声音,默认 "problem"(Pushcut 内置警示音)。
LIMIT_SOUND = os.environ.get("WATCH_LIMIT_SOUND", "problem").strip()

# Codex 额度预警阈值(%):任务完成时若订阅额度 used_percent ≥ 此值,完成通知会换成
# 「⚠️ 额度已用 NN%」的预警样式。设 0 或负数关闭预警。Claude 无此遥测,不适用。
try:
    LIMIT_WARN_PCT = float(os.environ.get("WATCH_LIMIT_WARN_PCT", "90"))
except ValueError:
    LIMIT_WARN_PCT = 90.0

# 通知正文末尾是否带「📁 项目文件夹名」(hook 输入 cwd 的 basename;Codex 不传 cwd 时
# 用 hook 进程自己的工作目录兜底)。多窗口并行时靠它分清是哪个窗口完成了任务;
# 设 WATCH_SHOW_CWD=0 关掉。与审批 hook 共用同一个开关。
SHOW_CWD = os.environ.get("WATCH_SHOW_CWD", "1").strip() != "0"


# ---------- 识别是哪个 agent 在调用:claude(默认)还是 codex ----------
# 同一份脚本同时服务 Claude Code 和 Codex CLI,两边通知用不同的标题和配图区分。
# 优先级:命令行 --agent(在 hooks 接线处显式声明,最可靠)> 环境变量 WATCH_AGENT > claude。
# Codex 侧 ~/.codex/hooks.json 的 command 带 "--agent codex";Claude 侧 settings.json 不带。
def _detect_agent():
    argv = sys.argv[1:]
    for i, a in enumerate(argv):
        if a == "--agent" and i + 1 < len(argv):
            return argv[i + 1].strip().lower()
        if a.startswith("--agent="):
            return a.split("=", 1)[1].strip().lower()
    return os.environ.get("WATCH_AGENT", "").strip().lower()


AGENT = _detect_agent()
if AGENT not in ("claude", "codex"):
    AGENT = "claude"
AGENT_NAME = "Codex" if AGENT == "codex" else "Claude"

# 每个 agent 的展示预设(标题/正文/配图;配图与审批 hook 同款,jsDelivr 锁定 commit)。
_CDN = "https://cdn.jsdelivr.net/gh/ghy196830-del/agent-watch-approve"
_AGENT_PRESETS = {
    "claude": {
        "title": "🦀 任务已完成",
        "text": "Claude 已完成当前任务",
        "image": _CDN + "@53b1672aff4f18f8e3581f83f92f079f3031d6e4/assets/clawd-crab.gif",
    },
    "codex": {
        "title": "🤖 任务已完成",
        "text": "Codex 已完成当前任务",
        "image": _CDN + "@45aa8e4deb6d68b33ac03206e27aebb8c8a8ab89/assets/gpt-cat.png",
    },
}
_PRESET = _AGENT_PRESETS[AGENT]

# 通知配图,按 agent 取预设(claude=螃蟹动图,codex=GPT 图标),与审批 hook 一致。
# 设 "none"/空 则不带图;想统一换图设 PUSHCUT_IMAGE=你的图片URL。
PUSHCUT_IMAGE = os.environ.get("PUSHCUT_IMAGE", _PRESET["image"]).strip()

# 是否标记为「限时通知 / Time-Sensitive」(冲破专注模式 + 更积极推到 Apple Watch)。
# 与审批 hook 共用同一个开关 PUSHCUT_TIME_SENSITIVE,默认开启;设为 0 关闭。
TIME_SENSITIVE = os.environ.get("PUSHCUT_TIME_SENSITIVE", "1").strip() != "0"

# 完成通知的标题 / 正文,默认按 agent 取预设,可用环境变量覆盖。
DONE_TITLE = os.environ.get("WATCH_DONE_TITLE", "").strip() or _PRESET["title"]
DONE_TEXT = os.environ.get("WATCH_DONE_TEXT", _PRESET["text"]).strip()

# 触发 Pushcut 的重试次数与单次超时(秒)。国内机场到 api.pushcut.io 的 TLS 握手会偶发
# 失败,重试几下基本就能成功;完成提醒不阻塞主流程,重试次数比审批略少即可。
try:
    PUSHCUT_RETRIES = max(1, int(os.environ.get("PUSHCUT_RETRIES", "8")))
except ValueError:
    PUSHCUT_RETRIES = 8
try:
    PUSHCUT_TIMEOUT = max(3, int(os.environ.get("PUSHCUT_TIMEOUT", "6")))
except ValueError:
    PUSHCUT_TIMEOUT = 6

# 通知名做 URL 转义,避免名字里有空格/特殊字符时拼坏 URL。
PUSHCUT_URL = "https://api.pushcut.io/v1/notifications/" + urllib.parse.quote(
    PUSHCUT_NOTIF, safe=""
)


def make_opener():
    """构造显式带机场代理的 opener;没有代理就直连(并屏蔽系统代理设置)。"""
    if PROXY:
        proxy_handler = urllib.request.ProxyHandler({"http": PROXY, "https": PROXY})
    else:
        proxy_handler = urllib.request.ProxyHandler({})
    return urllib.request.build_opener(proxy_handler)


def send_pushcut(opener, title, text, sound=None):
    """经代理 POST 触发 Pushcut 通知(无 actions = 无按钮);瞬时网络/TLS 失败自动重试。

    sound:不传用 DONE_SOUND;限额/异常类通知传 LIMIT_SOUND 以示区别。
    4xx(通知不存在=404、key 无效=401 等)是配置问题,重试无意义,直接 return。
    """
    snd = (sound if sound is not None else DONE_SOUND).strip()
    payload = {"title": title}
    if text:
        payload["text"] = text
    if PUSHCUT_DEVICES:
        payload["devices"] = PUSHCUT_DEVICES
    if snd and snd.lower() != "none":
        payload["sound"] = snd
    if PUSHCUT_IMAGE and PUSHCUT_IMAGE.lower() != "none":
        payload["image"] = PUSHCUT_IMAGE
    if TIME_SENSITIVE:
        payload["isTimeSensitive"] = True
    # 关键:不带 actions 字段 -> 通知上没有任何按钮,纯展示、不需回复。
    body = json.dumps(payload).encode("utf-8")

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
            # 4xx(429 限流除外)是配置错误,重试无意义,直接放弃(完成提醒不报错卡人)
            if 400 <= e.code < 500 and e.code != 429:
                return
        except Exception:
            pass
        if attempt < PUSHCUT_RETRIES - 1:
            time.sleep(0.3)


# ---------- 限额信息提取 ----------
# 限额文案特征(Claude:"You've hit your session limit ∙ resets 1:10am (Asia/Shanghai)";
# Codex:"You've hit your usage limit. Try again..."、"Rate limit reached" 等)。
_LIMIT_TEXT_RE = re.compile(r"hit your .{0,24}limit|usage limit|rate.?limit", re.I)
# 从限额文案里抽「重置时间」:resets 1:10am (Asia/Shanghai) / resets at 3am ...
_RESET_RE = re.compile(r"resets?\s*(?:at\s+)?[::]?\s*([^\r\n]+)", re.I)


def _fmt_epoch(epoch):
    """epoch 秒 -> 本地时间短格式(24h 内只给时分,否则带月日)。"""
    try:
        epoch = float(epoch)
        lt = time.localtime(epoch)
        if 0 < epoch - time.time() < 86400:
            return time.strftime("%H:%M", lt)
        return time.strftime("%m-%d %H:%M", lt)
    except Exception:
        return ""


def _window_label(minutes):
    """额度窗口时长 -> 人话标签。Codex 订阅常见:300=5小时滚动窗,10080=每周。"""
    try:
        minutes = int(minutes)
    except Exception:
        return ""
    if minutes >= 10080:
        return "周"
    if minutes >= 60:
        return "%d 小时" % round(minutes / 60.0)
    return "%d 分钟" % minutes


def read_codex_rate(transcript_path):
    """从 Codex rollout(transcript_path)尾部读最近一条 rate_limits 遥测。

    Codex 每次 token_count 事件都会带订阅额度状态:
      {"type":"event_msg","payload":{"type":"token_count","rate_limits":{
          "primary":{"used_percent":..,"window_minutes":..,"resets_at":..},
          "secondary":{...} 或 null, ...}}}
    返回 (used_percent, 窗口标签, 重置时间串) —— 取 primary/secondary 里用量更高的窗口;
    读不到返回 None。只读文件最后 ~256KB,够覆盖最近一轮。
    """
    if not transcript_path:
        return None
    try:
        size = os.path.getsize(transcript_path)
        with open(transcript_path, "rb") as f:
            f.seek(max(0, size - 262144))
            tail = f.read().decode("utf-8", "replace")
    except Exception:
        return None
    for line in reversed(tail.splitlines()):
        if '"rate_limits"' not in line:
            continue
        try:
            obj = json.loads(line)
        except Exception:
            continue  # 开头可能是被截断的半行
        payload = obj.get("payload") or {}
        rl = payload.get("rate_limits") or {}
        best = None
        for win in (rl.get("primary"), rl.get("secondary")):
            if not isinstance(win, dict):
                continue
            try:
                pct = float(win.get("used_percent"))
            except (TypeError, ValueError):
                continue
            if best is None or pct > best[0]:
                best = (pct, win)
        if best is None:
            return None
        pct, win = best
        return pct, _window_label(win.get("window_minutes")), _fmt_epoch(win.get("resets_at"))
    return None


def cwd_label(data):
    """hook 输入里的 cwd -> 项目文件夹名。多窗口并行时贴在正文末尾,
    分清「是哪个窗口完成了任务」。Codex 的 Stop 输入不带 cwd,
    退而取 hook 进程自己的工作目录(宿主 agent 启动 hook 时一般就是项目目录)。"""
    cwd = ""
    if isinstance(data, dict):
        cwd = str(data.get("cwd") or "").strip()
    if not cwd:
        try:
            cwd = os.getcwd()
        except Exception:
            return ""
    base = os.path.basename(cwd.rstrip("/\\"))
    return base or cwd


def build_notification(data):
    """根据 hook 输入决定这次发什么:返回 (title, text, sound)。

    * StopFailure(Claude 回合死于 API 错误):
        - 限额(error=rate_limit 或文案命中)-> 🚦 额度已用完 + 重置时间
        - 其它错误 -> ⚠️ 任务异常终止 + 错误类型
    * Stop:正常完成;Codex 额外检查订阅额度,过阈值则把完成通知换成预警样式。
    """
    event = str(data.get("hook_event_name") or "")

    if event == "StopFailure":
        err = str(data.get("error") or "").strip().lower()
        details = str(data.get("error_details") or "").strip()
        last = str(data.get("last_assistant_message") or "").strip()
        combined = " ".join(x for x in (last, details) if x)
        if err == "rate_limit" or _LIMIT_TEXT_RE.search(combined):
            # 重置时间分别在两个字段里找(不在拼接串里找,免得把后面的字段也吞进时间里)
            m = _RESET_RE.search(last) or _RESET_RE.search(details)
            if m:
                text = "限额将于 %s 重置" % m.group(1).strip().rstrip(".。")
            else:
                text = (combined or "订阅额度已用完,任务已中断")[:120]
            return "🚦 %s 额度已用完" % AGENT_NAME, text, LIMIT_SOUND
        text = err or "unknown"
        if details or last:
            text += ":" + (details or last)
        return "⚠️ %s 任务异常终止" % AGENT_NAME, text[:120], LIMIT_SOUND

    # 正常 Stop:Codex 顺带看一眼订阅额度,快烧完就把完成通知换成预警样式。
    if AGENT == "codex" and LIMIT_WARN_PCT > 0:
        rate = read_codex_rate(data.get("transcript_path"))
        if rate and rate[0] >= LIMIT_WARN_PCT:
            pct, label, reset = rate
            text = "任务已完成;%s额度已用 %.0f%%" % ((label + " ") if label else "", pct)
            if reset:
                text += ",%s 重置" % reset
            return "⚠️ Codex 额度已用 %.0f%%" % pct, text, LIMIT_SOUND

    return DONE_TITLE, DONE_TEXT, None


def main():
    # Stop / StopFailure 的 stdin 是一段 JSON(session_id / transcript_path / error 等)。
    try:
        raw = sys.stdin.buffer.read().decode("utf-8-sig", "replace").strip()
        data = json.loads(raw) if raw else {}
    except Exception:
        raw = ""
        data = {}

    # 调试留痕(需 WATCH_DEBUG_DUMP=1):本次 hook 输入覆盖写到
    # %TEMP%/watch_done_last_input_<agent>.json,排查「Stop hook 触发没有」直接看文件。
    if os.environ.get("WATCH_DEBUG_DUMP", "").strip() == "1":
        try:
            import tempfile
            with open(
                os.path.join(tempfile.gettempdir(), "watch_done_last_input_%s.json" % AGENT), "w",
                encoding="utf-8",
            ) as _f:
                _f.write(raw)
        except Exception:
            pass
    if not isinstance(data, dict):
        data = {}

    # 缺关键配置就静默退出(完成提醒是锦上添花,绝不打断 agent)。
    if not PUSHCUT_KEY:
        return

    title, text, sound = build_notification(data)
    # 多窗口并行:正文末尾带「📁 项目文件夹名」,分清是哪个窗口完成了任务。
    # \u00a0 = 不换行空格:防手表窄屏把 📁 和名字折成两行。
    if SHOW_CWD:
        folder = cwd_label(data)
        if folder:
            text = (text + "\n📁\u00a0" + folder) if text else "📁\u00a0" + folder
    send_pushcut(make_opener(), title, text, sound)
    # 不输出任何 JSON、正常 exit 0 -> agent 正常结束,不会触发 Stop 循环。


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception:
        # 兜底:任何意外都静默吞掉,绝不影响主流程。
        pass
    sys.exit(0)
