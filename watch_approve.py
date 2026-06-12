#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Claude Code / Codex CLI PreToolUse hook: 手表远程批准 (watch remote approval).

链路:
  stdin(JSON)
    -> 经机场代理 POST 触发 Pushcut 通知
    -> 我在 iPhone / Apple Watch 上点 Allow / Deny
    -> Pushcut 云端后台 POST 到 ntfy 的 topic (allow / deny)
    -> 本脚本经代理从 ntfy stream 读到结果
    -> 输出 permissionDecision 给 Claude Code

设计原则:
  * 只用 Python 3 标准库,不引第三方依赖。
  * 所有出网请求显式走 HTTPS_PROXY(机场本地代理)。
  * 配置全部从环境变量读,绝不硬编码密钥。
  * 任何配置缺失 / 异常 / 超时,一律 fail-safe 成 "ask"(退回终端正常弹窗),
    绝不让 hook 崩溃卡住 Claude Code。
  * 多窗口并行:每次审批用独立的回执 topic(基础 topic + 随机后缀),几个窗口
    同时等批准也不会串台;正文末尾带「📁 项目名」,一眼分清是谁在求批准。

自带 CLI(不当 hook 用时):
  * --doctor                逐项自检配置和链路(把 README 排错表自动跑一遍)
  * --print-claude-config   打印可直接粘贴的 Claude Code hooks 配置(绝对路径已转义)
  * --print-codex-config    打印可直接粘贴的 Codex hooks.json(同上)
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




# ---------- 兜底配置文件 watch.env ----------
# hook 进程的环境变量取决于「谁启动了 agent」:Claude Code 会把 settings.json 的 env
# 注入自己进程(hook 继承得到);但 Codex 不会把 config.toml 里 shell_environment_policy
# 的 env 传给 hook(那只作用于 shell 工具,见 codex-rs/hooks/engine/command_runner.rs)。
# 为了让脚本在任何宿主下都拿得到配置,这里读脚本同目录的 watch.env(KEY=VALUE 每行一条,
# # 开头是注释),【只填补缺失的环境变量】——真实环境变量永远优先。路径可用 WATCH_ENV_FILE 覆盖。
_ENV_FILE_PATH = ""    # 实际尝试读取的 watch.env 路径(--doctor 报告用)
_ENV_FILE_LOADED = False


def _load_env_file():
    global _ENV_FILE_PATH, _ENV_FILE_LOADED
    path = os.environ.get("WATCH_ENV_FILE", "").strip() or os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "watch.env"
    )
    _ENV_FILE_PATH = path
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
        _ENV_FILE_LOADED = True
    except Exception:
        pass


_load_env_file()


# ---------- 配置:全部来自环境变量 ----------
PUSHCUT_KEY = os.environ.get("PUSHCUT_KEY", "").strip()
PUSHCUT_NOTIF = os.environ.get("PUSHCUT_NOTIF", "claude").strip() or "claude"
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "").strip()

# 多窗口并行:ntfy 是发布/订阅,所有订阅同一 topic 的等待方会同时收到同一条回执——
# 若几个窗口共用一个 topic 一起等审批,你在 A 窗口通知上点的 ✅ 会把 B 窗口的操作一并
# 放行;过期通知上的迟到点击还会误批后来的请求。所以默认给每次审批生成独立回执 topic
# (基础 topic + "-" + 12 位随机后缀),按钮只对自己那次请求生效。设 WATCH_UNIQUE_TOPIC=0
# 退回旧的共享 topic 行为。注意:仅对动态按钮(PUSHCUT_DYNAMIC_ACTIONS=1,默认)生效;
# app 里手配的静态按钮指向固定 topic,只能共享(多窗口会串台,别用静态按钮跑多窗口)。
UNIQUE_TOPIC = os.environ.get("WATCH_UNIQUE_TOPIC", "1").strip() != "0"

# 通知正文末尾是否带「📁 项目文件夹名」(hook 输入 cwd 的 basename;Codex 不传 cwd 时
# 用 hook 进程自己的工作目录兜底)。多窗口并行时靠它分清是哪个窗口/项目在求批准;
# 单窗口嫌多一行可设 WATCH_SHOW_CWD=0 关掉。
SHOW_CWD = os.environ.get("WATCH_SHOW_CWD", "1").strip() != "0"

# 代理:优先 HTTPS_PROXY,其次大小写/HTTP 变体,形如 http://127.0.0.1:7890
PROXY = (
    os.environ.get("HTTPS_PROXY")
    or os.environ.get("https_proxy")
    or os.environ.get("HTTP_PROXY")
    or os.environ.get("http_proxy")
    or ""
).strip()

# 等待手表回执的总时长(秒)。必须 < .claude/settings.json 里 hook 的 timeout(300)。
try:
    APPROVE_WAIT = int(float(os.environ.get("APPROVE_WAIT", "240")))
except ValueError:
    APPROVE_WAIT = 240

# 超时(没等到回执)时按什么决策返回:allow / deny / ask(默认 ask 最安全)。
TIMEOUT_DECISION = os.environ.get("APPROVE_TIMEOUT_DECISION", "ask").strip().lower()
if TIMEOUT_DECISION not in ("allow", "deny", "ask"):
    TIMEOUT_DECISION = "ask"

# 是否由 hook 动态注入 Allow/Deny 两个后台按钮(需要 Pushcut Pro)。
# "1"(默认):你只需在 Pushcut app 里建一条名为 PUSHCUT_NOTIF 的空通知即可,
#             按钮和它指向的 ntfy URL 都由 hook 在 API 调用里带上。
# "0":不注入,改用你在 app 里手动配好的 action。
DYNAMIC_ACTIONS = os.environ.get("PUSHCUT_DYNAMIC_ACTIONS", "1").strip() != "0"

# 指定通知发给哪些 Pushcut 设备(设备名见 GET /v1/devices)。逗号分隔,例如 "iPhone,watch"。
# 留空 = 用 Pushcut 默认(发给所有设备)。直接点名 watch 可绕过「iPhone 没锁屏就不镜像到表」
# 的 Apple 规则,确保手表能直接收到。
PUSHCUT_DEVICES = [
    d.strip() for d in os.environ.get("PUSHCUT_DEVICES", "").split(",") if d.strip()
]

# 通知声音。Pushcut 不带 sound 时会被当成静默通知,手表/手机不会震动提醒。
# 设成 "default" 让它正式提醒(手表会震)。想只震不响可用 "vibrateOnly";
# 其它可选值见 Pushcut(system/subtle/question/jobDone/problem/loud 等)。设 "none" 则不带。
PUSHCUT_SOUND = os.environ.get("PUSHCUT_SOUND", "default").strip()

# 是否把通知标记为「限时通知 / Time-Sensitive」。开启后:① 能冲破 iPhone 的专注模式/勿扰;
# ② Apple 会更积极地把它推到 Apple Watch——这是绕过「iPhone 在用时通知只显示在手机、
# 不上手表」这条系统路由规则的最有效手段(纯 API 侧能做的极限)。审批本就是时效操作,
# 默认开启;设 PUSHCUT_TIME_SENSITIVE=0 可关。
TIME_SENSITIVE = os.environ.get("PUSHCUT_TIME_SENSITIVE", "1").strip() != "0"

# ---------- AskUserQuestion:把终端里的多选决策框也搬上手表 ----------
# Claude 有时不是要批准、而是抛一道选择题(AskUserQuestion 工具,在终端渲染成选项框)。
# 它没有 command/file_path,danger-only 模式会把它当「非危险」静默放行——结果题目只在
# 终端干等,人不在电脑前任务就卡死。默认把「单问题、单选」的题推到手表:正文 = 问题 +
# A/B/C/D 选项摘要,按钮 = 方案A.../🖥️ 在终端查看(选项全文太长,按钮只放代号)。
#   点「方案X」      -> 选择回传给 Claude,按该选项直接继续,终端不再弹框;
#   点「在终端查看」/ 超时 / 多问题 / 多选题 -> 放行,题目照常在终端弹出,交互不丢。
# 设 WATCH_ASK_QUESTIONS=0 关闭(回到旧行为:静默放行、只在终端选)。
ASK_QUESTIONS = os.environ.get("WATCH_ASK_QUESTIONS", "1").strip() != "0"
QUESTION_TITLE = os.environ.get("WATCH_QUESTION_TITLE", "").strip() or "🤔 Claude 在问你"
# 选择题通知的声音,和审批区分开(Pushcut 内置音里 "question" 很贴脸;none=不带)。
QUESTION_SOUND = os.environ.get("WATCH_QUESTION_SOUND", "question").strip()
_OPT_LETTERS = "ABCD"  # AskUserQuestion 一题最多 4 个选项
_OPT_LABEL_MAX = 24    # 正文里每个选项标签最多保留的字符数(约手表一行的量)

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

# 每个 agent 的展示预设。配图都是贴边裁好的矮横幅(777x243 透明背景,通知里图矮、
# 批准按钮不用下滑),走 jsDelivr CDN(国内可访问),锁定到具体 commit 防止失效。
_CDN = "https://cdn.jsdelivr.net/gh/ghy196830-del/agent-watch-approve"
_AGENT_PRESETS = {
    "claude": {
        "title": "🦀 Claude 待批准",
        # Claude Code 官方像素螃蟹 Clawd,动图(48 帧,会眨眼挪腿;手表上只显示静帧)。
        # 同仓库另有透明静态 PNG 兜底:.../assets/clawd-crab.png。
        "image": _CDN + "@53b1672aff4f18f8e3581f83f92f079f3031d6e4/assets/clawd-crab.gif",
    },
    "codex": {
        "title": "🤖 Codex 待批准",
        # GPT 结猫(用户自选吉祥物:OpenAI 结 logo 化身小猫),透明背景,与螃蟹同规格横幅。
        # 同仓库另有 ChatGPT 官方圆角图标备选:.../assets/gpt-logo.png。
        "image": _CDN + "@45aa8e4deb6d68b33ac03206e27aebb8c8a8ab89/assets/gpt-cat.png",
    },
}
_PRESET = _AGENT_PRESETS[AGENT]

# 通知配图(Pushcut 的 image 字段:公开图片 URL,或 Pushcut app 里存的图片名)。
# 默认按上面识别出的 agent 取预设(claude=螃蟹动图,codex=GPT 图标)。
# 设成 "none"/空 则不带图;想统一换图设 PUSHCUT_IMAGE=你的图片URL(对两个 agent 都生效)。
PUSHCUT_IMAGE = os.environ.get("PUSHCUT_IMAGE", _PRESET["image"]).strip()

# 通知正文最多显示多少字符,超出截断。手表屏幕小,默认压到 80 字,一行就够;
# 想再短/再长设环境变量 WATCH_DESC_MAX。
try:
    DESC_MAX = max(20, int(os.environ.get("WATCH_DESC_MAX", "80")))
except ValueError:
    DESC_MAX = 80

# 审批通知的第三个按钮「🖥️ 终端查看」:点了返回 ask——Claude 退回终端原生弹窗、
# Codex 弹它自己的审批。命令太复杂、想回电脑看全文再决定时用。
# 设 WATCH_TERMINAL_BUTTON=0 只留 ✅/❌ 两个按钮。
TERMINAL_BUTTON = os.environ.get("WATCH_TERMINAL_BUTTON", "1").strip() != "0"

# 通知正文默认只有「人话标签 + 目标名」(手表屏幕小);设 WATCH_SHOW_RAW=1 在正文末尾
# 附原始详情(Bash/PowerShell=原始命令,写类工具=完整路径),单独成行,
# 截断到 WATCH_RAW_MAX 字符。
SHOW_RAW = os.environ.get("WATCH_SHOW_RAW", "0").strip() == "1"
try:
    RAW_MAX = max(20, int(os.environ.get("WATCH_RAW_MAX", "160")))
except ValueError:
    RAW_MAX = 160

# 触发 Pushcut 通知的重试次数。经实测国内机场到 api.pushcut.io 的 TLS 握手会偶发
# SSLEOFError,重试几下基本就能成功(瞬时失败很快,不会拖很久)。
try:
    PUSHCUT_RETRIES = max(1, int(os.environ.get("PUSHCUT_RETRIES", "12")))
except ValueError:
    PUSHCUT_RETRIES = 12

# 单次触发 Pushcut 的超时(秒)。设短一点,这样卡住的连接能尽快失败、转下一次重试,
# 减少“通知迟迟不到”的体感延迟。SSLEOFError 本身是秒级失败,不受此影响。
# 默认 3s:实测国内机场到 api.pushcut.io,成功的握手 ~1.8s、失败的要卡满 ~5s 才报错,
# 约一半会失败;把超时压到 3s(成功留足余量)能在失败连接卡满 5s 之前就掐掉去重试,
# 省下 ~2s/次。想再宽松/严格设环境变量 PUSHCUT_TIMEOUT。
try:
    PUSHCUT_TIMEOUT = max(3, int(os.environ.get("PUSHCUT_TIMEOUT", "3")))
except ValueError:
    PUSHCUT_TIMEOUT = 3

# ntfy 服务地址,自建实例用 NTFY_BASE 覆盖(默认公共 ntfy.sh,尾斜杠可写可不写)。
# 注意:手表按钮是 Pushcut 云端发的「无自定义 header 的 GET」,带不了 Authorization——
# 自建实例必须允许该 topic 匿名读写,否则按钮发不进去;公共 ntfy.sh + 长随机 topic 最顺滑。
NTFY_BASE = (os.environ.get("NTFY_BASE", "").strip() or "https://ntfy.sh/").rstrip("/") + "/"
# 通知名做 URL 转义,避免名字里有空格/特殊字符时拼坏 URL。
PUSHCUT_URL = "https://api.pushcut.io/v1/notifications/" + urllib.parse.quote(
    PUSHCUT_NOTIF, safe=""
)

# 读 ntfy stream 时单次 socket 超时;要大于 ntfy 的 keepalive(默认 ~45s),
# 这样正常等待期间不会被误判成断线。它只是断线兜底,不是总超时。
STREAM_READ_TIMEOUT = 60

# 当前 hook 事件名(main 里从输入 JSON 的 hook_event_name 读出)。决定 emit() 的输出格式:
#   * Claude 的 PreToolUse            -> permissionDecision 格式
#   * Codex 的 PermissionRequest      -> decision.behavior 格式(Codex 专属事件,只在
#     Codex 自己要弹审批时触发;hook 不回应 = 走 Codex 正常审批流程,等同 ask)
HOOK_EVENT = ""


# ---------- 危险命令过滤:只把"真要命"的操作推到手表 ----------
# WATCH_DANGER_ONLY=1 时,凡是没命中危险模式的操作,直接返回 "ask"(退回 agent 正常
# 流程),不打扰手表。这样日常命令安静,只有真正危险的操作才震你手表。
DANGER_ONLY = os.environ.get("WATCH_DANGER_ONLY", "0").strip() == "1"

# danger-only 模式下,非危险操作怎么处理:
#   ask(默认)= 退回 Claude Code 正常流程(终端可能还会问你 yes)
#   allow      = 直接自动放行,既不弹手表也不在终端问(危险才上手表、其余全自动)
#   deny       = 直接拒绝
NONDANGER_DECISION = os.environ.get("WATCH_NONDANGER_DECISION", "ask").strip().lower()
if NONDANGER_DECISION not in ("allow", "deny", "ask"):
    NONDANGER_DECISION = "ask"

_DEFAULT_DANGER_PATTERNS = [
    r"\brm\s+-",                                       # rm 带任意 flag(rm -rf / rm -r ...)
    r"\bsudo\b",
    r"\bgit\s+push\b.*(--force|-f|\s\+)",              # 强推
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
    r":\(\)\s*\{",                                     # fork 炸弹
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
# WATCH_DANGER_REGEX 整体覆盖默认清单;WATCH_DANGER_EXTRA(换行分隔)在默认基础上追加。
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
    """text 命中任一危险模式则返回 True。"""
    if not text:
        return False
    for rx in _DANGER_RE:
        if rx.search(text):
            return True
    return False


# ---------- 受保护脚本路径:对它们的「写操作」强制上手表 ----------
# 背景:Claude Code 对 .claude/ 里的文件(hook 脚本、settings)有一道写死的终端确认,
# hook 的 allow / permissions.allow / 连 --dangerously-skip-permissions 都压不住(见
# github issue #41615),所以「改 .claude/ 里的 hook」没法走手表。对策:把 hook 脚本搬到
# .claude/ 之外,再用这里的规则——凡是**写类工具**动到这些路径,一律当危险、路由到手表。
# 逗号分隔的子串(大小写不敏感),命中即算;留空(默认)= 关闭。本机用 WATCH_PROTECT_PATHS
# 设成 "watch-hooks"(脚本新家目录名),这样改 C:\Users\ghy19\watch-hooks\*.py 就走手表。
PROTECT_PATHS = [
    p.strip().lower()
    for p in os.environ.get("WATCH_PROTECT_PATHS", "").split(",")
    if p.strip()
]
# 只对「写类」工具应用受保护路径规则:Read/Glob 等只读工具即使 file_path 命中也不拦
# (否则每次读 hook 脚本都震你手表)。
_WRITE_TOOLS = ("Write", "Edit", "MultiEdit", "NotebookEdit")

# 在 WATCH_PROTECT_PATHS(用户手填子串)之外,**默认**再保护脚本自身:agent 改掉
# watch_approve.py / watch.env 就能解除你的管控,所以写类工具动到本脚本所在目录,
# 一律视同危险、强制上手表。用规范化绝对路径比较(Windows 大小写不敏感),不靠子串。
# 设 WATCH_PROTECT_SELF=0 关闭。路径解析失败按「不命中」处理(fail-open),
# 与全脚本 fail-safe 原则一致,绝不因此把 agent 卡死。
# 已知边界:Bash 里 `echo x > watch_approve.py` 这类重定向不走写类工具,拦不到。
PROTECT_SELF = os.environ.get("WATCH_PROTECT_SELF", "1").strip() != "0"


def _norm_abs(path, base=""):
    """规范化成绝对路径(normcase:Windows 下统一小写+反斜杠);失败返回空串。
    相对路径先按 base(hook 输入的 cwd)落地,没有 base 才用进程工作目录。"""
    try:
        p = str(path)
        if not os.path.isabs(p) and base:
            p = os.path.join(base, p)
        return os.path.normcase(os.path.abspath(p))
    except Exception:
        return ""


_SELF_DIR = _norm_abs(os.path.dirname(os.path.abspath(__file__))) if PROTECT_SELF else ""


def is_protected_write(tool_name, tool_input, cwd=""):
    """写类工具动到受保护路径(WATCH_PROTECT_PATHS 子串 / 脚本自身目录)-> True(该走手表)。"""
    if tool_name not in _WRITE_TOOLS or not isinstance(tool_input, dict):
        return False
    fp = str(tool_input.get("file_path") or tool_input.get("notebook_path") or "")
    if not fp:
        return False
    if PROTECT_PATHS and any(p in fp.lower() for p in PROTECT_PATHS):
        return True
    if _SELF_DIR:
        rp = _norm_abs(fp, cwd)
        if rp and (rp == _SELF_DIR or rp.startswith(_SELF_DIR + os.sep)):
            return True
    return False


# ---------- 会被 Claude Code 强制弹终端的路径:hook 拦不住,只能提醒 ----------
# Claude Code 对 .claude/ 里的 settings / hook 文件有一道写死的终端确认,hook 返回 allow
# 也压不住(github issue #41615),连 --dangerously-skip-permissions 都没用。对这类操作,
# hook 改变不了「必须去终端点」的事实,但它仍会先触发(#41615:"Hook fires but prompt
# still shows"),所以能**给手表/手机推一条无按钮提醒**:"去终端确认",让你不至于对着
# 静默卡住的终端发呆。匹配子串(大小写不敏感,逗号分隔)。
# 默认只点名 .claude 下的 settings/hooks —— **特意不写成整个 ".claude"**,否则会误伤
# ~/.claude/projects/**/memory/(记忆文件,根本不弹终端)和搬出去的 watch-hooks。
# 想加别的强制终端路径用 WATCH_TERMINAL_FORCED_PATHS 覆盖;设空串则关闭整套提醒。
TERMINAL_FORCED_PATHS = [
    p.strip().lower()
    for p in os.environ.get(
        "WATCH_TERMINAL_FORCED_PATHS",
        ".claude\\settings.json,.claude/settings.json,"
        ".claude\\settings.local.json,.claude/settings.local.json,"
        ".claude\\hooks,.claude/hooks",
    ).split(",")
    if p.strip()
]


def is_terminal_forced(tool_name, tool_input):
    """该操作会被 Claude Code 强制弹终端(hook 拦不住)-> True(只提醒、不做手表审批)。"""
    if not TERMINAL_FORCED_PATHS or not isinstance(tool_input, dict):
        return False
    if tool_name in _WRITE_TOOLS:
        text = str(tool_input.get("file_path") or tool_input.get("notebook_path") or "")
    elif tool_name in ("Bash", "PowerShell"):
        text = str(tool_input.get("command") or "")
    else:
        return False
    text = text.lower()
    return bool(text) and any(p in text for p in TERMINAL_FORCED_PATHS)


# ---------- 危险操作 -> 简短中文确认标签 ----------
# 推到手表的正文不再堆原始命令(一长串 + D 盘全路径看着累),而是一句「让我干什么」的
# 确认:命中的危险类别给一个中文标签(下表),再附上操作目标的「文件/文件夹名」(只取
# basename,去掉盘符和长目录)。每项 = (正则, 标签),命令命中第一条就用它的标签。
# 下表覆盖了 _DEFAULT_DANGER_PATTERNS 的每一条;若你用 WATCH_DANGER_REGEX / _EXTRA
# 自定义了别的危险模式,这里匹配不到标签时,describe() 会优雅退回「截断后的简短命令」。
_DANGER_LABELS = [
    (r"\bremove-item\b.*-recurse.*-force", "🗑️ 删除文件夹"),
    (r"\bdel\b\s+/[sf]", "🗑️ 删除文件"),
    (r"\brm\s+-", "🗑️ 删除文件/目录"),
    (r"\bformat\b\s+[a-z]:", "💽 格式化磁盘"),
    (r"\bmkfs\b", "💽 格式化文件系统"),
    (r"\bdd\b.*\bif=", "💽 dd 写盘"),
    (r"\bof=/dev/", "💽 写入磁盘设备"),
    (r">\s*/dev/sd", "💽 写入磁盘设备"),
    (r"\bsudo\b", "🔑 sudo 提权执行"),
    (r"\bgit\s+push\b.*(--force|-f|\s\+)", "⚠️ git 强制推送"),
    (r"\bgit\s+reset\s+--hard\b", "↩️ git 丢弃改动(reset --hard)"),
    (r"\bgit\s+clean\s+-[a-z]*f", "🧹 git 清理未跟踪文件"),
    (r"\b(shutdown|reboot|halt|poweroff)\b", "🔌 关机/重启"),
    (r"\b(kill|pkill|killall)\b", "🛑 结束进程"),
    (r"\bchmod\s+(-r\s+|.*\b777\b)", "🔓 修改权限(chmod)"),
    (r"\bchown\s+-r\b", "👤 修改属主(chown -R)"),
    (r":\(\)\s*\{", "💥 疑似 fork 炸弹"),
    (r"\b(drop|truncate)\s+(table|database)\b", "🗄️ 删表/清库"),
    (r"\bdelete\s+from\b", "🗄️ 删除数据(DELETE FROM)"),
    (r"(curl|wget)\b.*\|\s*(sudo\s+)?(sh|bash|zsh)\b", "📥 下载并执行脚本"),
    (r"\bnpm\s+publish\b", "📦 发布 npm 包"),
    (r"\bdocker\b.*\b(rm|prune|down)\b", "🐳 删除 docker 资源"),
    (r"\bterraform\s+destroy\b", "💥 terraform 销毁资源"),
    (r"\bkubectl\s+delete\b", "☸️ kubectl 删除资源"),
]
_DANGER_LABEL_RE = []
for _p, _lab in _DANGER_LABELS:
    try:
        _DANGER_LABEL_RE.append((re.compile(_p, re.IGNORECASE), _lab))
    except re.error:
        pass

# 像路径的 token:X:\... 盘符路径,或任何含 / 或 \ 的串。用来从命令里抠出「目标」。
_PATH_TOKEN_RE = re.compile(r"""[A-Za-z]:[\\/][^\s"']*|[^\s"']*[\\/][^\s"']*""")


def danger_label(text):
    """命中的危险类别 -> 简短中文标签;没命中返回 None。"""
    if not text:
        return None
    for rx, lab in _DANGER_LABEL_RE:
        if rx.search(text):
            return lab
    return None


def short_target(command):
    """从命令里抽一个『目标』给确认用:取最后一个像路径的 token,只留 basename
    (去掉盘符 / 长目录,手表上只看到文件/文件夹名)。抽不到返回空串。"""
    if not command:
        return ""
    tokens = _PATH_TOKEN_RE.findall(command)
    if not tokens:
        return ""
    raw = tokens[-1].strip().strip("\"'")
    base = os.path.basename(raw.rstrip("/\\"))
    return base or raw


def cwd_label(data):
    """hook 输入里的 cwd -> 项目文件夹名。多窗口并行时贴在正文末尾,
    分清「是哪个窗口/项目在求批准」。Codex 的 hook 输入不带 cwd,
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


def emit(decision, reason, updated_input=None):
    """向 stdout 输出 hook JSON 并 exit 0。decision ∈ allow | deny | ask。

    输出格式按事件区分:
      * Codex 的 PermissionRequest:用 decision.behavior 格式;其中「ask=什么都不输出」
        (Codex 收不到决定就走它自己的正常审批流程,语义上等同 Claude 的 ask)。
      * 其余(Claude 的 PreToolUse 等):permissionDecision 格式。
    updated_input:非 None 时随 allow 一起输出 updatedInput(官方机制:在工具执行前
    替换其参数;AskUserQuestion 靠它回填 answers,见 handle_question)。仅 PreToolUse 格式支持。
    """
    if HOOK_EVENT == "PermissionRequest":
        if decision == "ask":
            sys.exit(0)
        beh = (
            {"behavior": "allow"}
            if decision == "allow"
            else {"behavior": "deny", "message": reason}
        )
        out = {"hookSpecificOutput": {"hookEventName": "PermissionRequest", "decision": beh}}
    else:
        hso = {
            "hookEventName": "PreToolUse",
            "permissionDecision": decision,
            "permissionDecisionReason": reason,
        }
        if updated_input is not None:
            hso["updatedInput"] = updated_input
        out = {"hookSpecificOutput": hso}
    # 显式以 UTF-8 字节写出,避免 Windows 控制台 codepage(如 GBK/cp936)把
    # permissionDecisionReason 里的中文编错,导致 Claude Code 读到乱码。
    payload = json.dumps(out, ensure_ascii=False).encode("utf-8")
    try:
        sys.stdout.buffer.write(payload)
        sys.stdout.buffer.flush()
    except Exception:
        sys.stdout.write(json.dumps(out, ensure_ascii=False))
        sys.stdout.flush()
    sys.exit(0)


def make_opener():
    """构造显式带机场代理的 opener;没有代理就直连(并屏蔽系统代理设置)。"""
    if PROXY:
        proxy_handler = urllib.request.ProxyHandler({"http": PROXY, "https": PROXY})
    else:
        proxy_handler = urllib.request.ProxyHandler({})
    return urllib.request.build_opener(proxy_handler)


# 工具名 -> 中文动词前缀,让正文一眼是中文(命令本身是代码,没法翻译,只能带原文)。
_CN_VERB = {
    "Bash": "运行命令:",
    "PowerShell": "运行命令:",
    "Write": "写入文件:",
    "Edit": "编辑文件:",
    "MultiEdit": "批量编辑:",
    "NotebookEdit": "编辑笔记本:",
}


def describe(tool_name, tool_input):
    """拼一句简短的中文确认正文(给手表看「在让我批准什么」,不堆原始命令/长路径)。

    Bash/PowerShell:不回显整条命令,而是「危险动作标签 + 目标文件名」,
        例如『🗑️ 删除文件夹:.watch_danger_test』。命中不到标签(自定义危险正则)
        才退回截断后的简短命令兜底。
    Write/Edit/MultiEdit/NotebookEdit:只显示 basename(同样不带长路径)。
    其它工具:理论上非危险、走不到这里(已在 main 里放行),兜底给个 JSON 摘要。
    """
    ti = tool_input if isinstance(tool_input, dict) else {}

    # Codex 的文件修改工具 apply_patch:tool_input.command 是补丁全文(很长),
    # 不回显,只抽出补丁里涉及的文件名(*** Add/Update/Delete File: 行)。
    if tool_name == "apply_patch":
        cmd = str(ti.get("command", ""))
        files = re.findall(r"\*\*\*\s*(?:Add|Update|Delete) File:\s*(.+)", cmd)
        names = "、".join(
            os.path.basename(f.strip().strip("\"'").rstrip("/\\")) for f in files[:3] if f.strip()
        )
        desc = ("📝 修改文件:" + names) if names else "📝 应用代码补丁"
        if len(desc) > DESC_MAX:
            desc = desc[: DESC_MAX - 1] + "…"
        return desc

    if tool_name in ("Bash", "PowerShell"):
        cmd = " ".join(str(ti.get("command", "")).split())  # 压平空白/换行
        label = danger_label(cmd)
        if label:
            tgt = short_target(cmd)
            desc = (label + ":" + tgt) if tgt else label
        else:
            desc = (_CN_VERB.get(tool_name, "操作:") + cmd) if cmd else "运行命令"
        if len(desc) > DESC_MAX:
            desc = desc[: DESC_MAX - 1] + "…"
        # WATCH_SHOW_RAW=1:人话之外再附原始命令(单独一行,独立的截断上限)。
        if SHOW_RAW and cmd:
            raw = cmd if len(cmd) <= RAW_MAX else cmd[: RAW_MAX - 1] + "…"
            desc += "\n$ " + raw
        return desc

    fp_raw = ""
    if tool_name in ("Write", "Edit", "MultiEdit"):
        fp_raw = str(ti.get("file_path", ""))
        body = os.path.basename(fp_raw.rstrip("/\\"))
    elif tool_name == "NotebookEdit":
        fp_raw = str(ti.get("notebook_path", ""))
        body = os.path.basename(fp_raw.rstrip("/\\"))
    else:
        try:
            body = json.dumps(ti, ensure_ascii=False)
        except Exception:
            body = str(ti)
    body = " ".join(body.split())  # 压平空白/换行成一行
    # 受保护路径(含脚本自身目录)的写操作:用 🛡️ 点明「这是在改 hook 脚本」。
    if is_protected_write(tool_name, ti):
        prefix = "🛡️ 改 hook 脚本:"
    else:
        prefix = _CN_VERB.get(tool_name, "操作:")
    desc = (prefix + body) if body else prefix.rstrip(":")
    if len(desc) > DESC_MAX:
        desc = desc[: DESC_MAX - 1] + "…"
    # WATCH_SHOW_RAW=1:basename 之外再附完整路径(单独一行)。
    if SHOW_RAW and fp_raw:
        raw = " ".join(fp_raw.split())
        if len(raw) > RAW_MAX:
            raw = raw[: RAW_MAX - 1] + "…"
        desc += "\n" + raw
    return desc


def make_reply_topic():
    """生成本次审批的回执 topic:多窗口下每次请求独立,互不串台。

    动态按钮 + 未关闭 WATCH_UNIQUE_TOPIC 时 = 基础 topic + "-" + 12 位随机十六进制;
    否则(静态按钮指向固定 topic / 显式要求共享)退回基础 topic,行为与旧版完全一致。
    """
    if UNIQUE_TOPIC and DYNAMIC_ACTIONS:
        return NTFY_TOPIC + "-" + os.urandom(6).hex()
    return NTFY_TOPIC


def build_actions(reply_topic):
    """动态生成审批按钮:✅ 允许 / ❌ 拒绝(+ 可选 🖥️ 终端查看),用「后台 web 请求」
    GET 方式 publish 到 ntfy 的 reply_topic(本次审批专属 topic,见 make_reply_topic)。

    关键:必须用 urlBackgroundOptions(后台 web 请求),不能用普通 url(=打开链接/打开
    app)——watchOS 不支持「打开 app / 跑快捷指令」类动作,只支持后台 web 请求。
    用 GET(ntfy 支持 /publish?message=xxx)可避开 urlBackgroundOptions 里 httpBody
    偶发不生效的问题。返回 None 表示不注入(改用 app 里手配的 action)。
    """
    if not DYNAMIC_ACTIONS or not reply_topic:
        return None
    base = NTFY_BASE + urllib.parse.quote(reply_topic, safe="") + "/publish?message="
    acts = [
        {"name": "✅ 允许", "url": base + "allow", "urlBackgroundOptions": {"httpMethod": "GET"}},
        {"name": "❌ 拒绝", "url": base + "deny", "urlBackgroundOptions": {"httpMethod": "GET"}},
    ]
    if TERMINAL_BUTTON:
        # 第三个按钮:既不放行也不拒绝,退回终端审批(Claude=ask,Codex=原生弹窗)。
        acts.append(
            {"name": "🖥️ 终端查看", "url": base + "term", "urlBackgroundOptions": {"httpMethod": "GET"}}
        )
    return acts


def parse_question(tool_input):
    """AskUserQuestion 的 tool_input -> 手表可处理的「单选题」结构;处理不了返回 None。

    可处理 = 恰好 1 个问题、非 multiSelect、2~4 个选项(工具本身上限就是 4)。
    多问题 / 多选题一条通知放不下(一块表盘塞不进两道题),交回终端。
    返回 {"question": 展示用问题文本, "question_raw": 问题原文,
          "labels": [展示用 label(压平空白)], "labels_raw": [label 原文]}。
    *_raw 用于 answers 回填(updatedInput 里的 key/value 必须和工具输入原文一致),
    展示字段只给通知正文用。
    """
    if not isinstance(tool_input, dict):
        return None
    qs = tool_input.get("questions")
    if not isinstance(qs, list) or len(qs) != 1 or not isinstance(qs[0], dict):
        return None
    q = qs[0]
    if q.get("multiSelect"):
        return None
    opts = q.get("options")
    if not isinstance(opts, list) or not 2 <= len(opts) <= len(_OPT_LETTERS):
        return None
    labels, labels_raw = [], []
    for i, o in enumerate(opts):
        raw = str((o if isinstance(o, dict) else {}).get("label") or "")
        lab = " ".join(raw.split())
        labels.append(lab or ("选项%d" % (i + 1)))
        labels_raw.append(raw or ("选项%d" % (i + 1)))
    raw_q = str(q.get("question") or "")
    text = " ".join(raw_q.split()) or "(问题为空)"
    return {"question": text, "question_raw": raw_q, "labels": labels, "labels_raw": labels_raw}


def question_body(q):
    """选择题通知正文:问题一行 + 每个选项一行「A. label」。

    按钮上只有「方案A/方案B」代号,所以正文必须给出 A/B 对应什么,不然没法盲点;
    标签超过 _OPT_LABEL_MAX 截断(手表屏幕窄,正文可以滚动但别太啰嗦)。
    """
    head = q["question"]
    if len(head) > DESC_MAX:
        head = head[: DESC_MAX - 1] + "…"
    lines = [head]
    for i, lab in enumerate(q["labels"]):
        if len(lab) > _OPT_LABEL_MAX:
            lab = lab[: _OPT_LABEL_MAX - 1] + "…"
        lines.append("%s. %s" % (_OPT_LETTERS[i], lab))
    return "\n".join(lines)


def build_question_actions(reply_topic, n):
    """选择题按钮:方案A..方案D + 「🖥️ 在终端查看」,同样全是后台 web 请求(watchOS 限制)。

    点「方案X」后台 publish "optX" 到回执 topic;点「在终端查看」publish "term"
    (= 放行,让题目在终端正常弹出,适合人就在电脑前的场景)。
    """
    base = NTFY_BASE + urllib.parse.quote(reply_topic, safe="") + "/publish?message="
    acts = [
        {
            "name": "方案" + _OPT_LETTERS[i],
            "url": base + "opt" + _OPT_LETTERS[i],
            "urlBackgroundOptions": {"httpMethod": "GET"},
        }
        for i in range(n)
    ]
    acts.append(
        {"name": "🖥️ 在终端查看", "url": base + "term", "urlBackgroundOptions": {"httpMethod": "GET"}}
    )
    return acts


def send_pushcut(opener, title, text, with_actions=True, retries=None, reply_topic=None,
                 actions=None, sound=None):
    """经代理 POST 触发 Pushcut 通知;对瞬时网络/TLS 失败自动重试。

    with_actions=False:不带 Allow/Deny 按钮,纯提醒(用于「去终端确认」这种无需回执的通知)。
    reply_topic:按钮 publish 的回执 topic(多窗口下本次审批专属;缺省用基础 topic)。
    retries:本次最多尝试几次(默认用 PUSHCUT_RETRIES);提醒类通知传小一点,避免拖慢终端。
    actions:直接指定按钮列表(选择题的「方案A/B/...」),非 None 时优先于 with_actions。
    sound:覆盖本条通知的声音(选择题用 question 音);None = 用全局 PUSHCUT_SOUND。
    4xx(如通知不存在=404、key 无效=401)是配置问题,重试也没用,直接抛出 ->
    上层会 fail-safe 成 ask。
    """
    payload = {"title": title, "text": text}
    if actions is not None:
        payload["actions"] = actions
    elif with_actions:
        acts = build_actions(reply_topic or NTFY_TOPIC)
        if acts:
            payload["actions"] = acts
    if PUSHCUT_DEVICES:
        payload["devices"] = PUSHCUT_DEVICES
    snd = sound if sound is not None else PUSHCUT_SOUND
    if snd and snd.lower() != "none":
        payload["sound"] = snd
    if PUSHCUT_IMAGE and PUSHCUT_IMAGE.lower() != "none":
        payload["image"] = PUSHCUT_IMAGE
    if TIME_SENSITIVE:
        payload["isTimeSensitive"] = True
    body = json.dumps(payload).encode("utf-8")

    last = None
    n = retries or PUSHCUT_RETRIES
    for attempt in range(n):
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
            # 4xx(429 限流除外)是配置错误,重试无意义,立刻上抛
            if 400 <= e.code < 500 and e.code != 429:
                raise
            last = e
        except Exception as e:
            last = e
        if attempt < n - 1:
            time.sleep(0.3)
    if last is not None:
        raise last


def wait_for_decision(opener, since_ts, deadline, topic=None, tokens=None):
    """从 ntfy stream 读回执。到 deadline 没读到返回 None。

    默认(tokens=None)认审批语义:读到 allow/deny(及同义词)返回 'allow'/'deny',
    读到 term(「🖥️ 终端查看」按钮)返回 'term'。
    tokens 非空(选择题)时改认指定 token 集(如 {"opta","optb","term"},全小写),
    读到集合内的消息原样返回该 token,其余内容忽略。
    topic:本次审批的回执 topic(缺省用基础 topic,兼容旧行为)。
    用 since=since_ts(发通知前记下的 t0)防竞态:即使秒点、回执比订阅先到,
    重连/订阅时 ntfy 会把 t0 之后的消息一并回放,不会漏。
    """
    url = (
        NTFY_BASE
        + urllib.parse.quote(topic or NTFY_TOPIC, safe="")
        + "/json?since="
        + str(since_ts)
    )
    while time.monotonic() < deadline:
        try:
            resp = opener.open(url, timeout=STREAM_READ_TIMEOUT)
        except Exception:
            time.sleep(1)  # 打开失败,稍后重连(不超过 deadline)
            continue
        try:
            while time.monotonic() < deadline:
                try:
                    raw = resp.readline()
                except socket.timeout:
                    break  # 本次连接静默太久,跳出去重连
                except Exception:
                    break
                if not raw:
                    break  # 服务端关闭连接,重连
                line = raw.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line.decode("utf-8", "replace"))
                except Exception:
                    continue
                if obj.get("event") != "message":
                    continue  # 只认 message,忽略 open / keepalive
                msg = (obj.get("message") or "").strip().lower()
                if tokens is not None:
                    if msg in tokens:
                        return msg
                    continue  # 选择题只认自己的 token,其它内容忽略
                if msg in ("allow", "approve", "yes", "ok"):
                    return "allow"
                if msg in ("deny", "block", "no"):
                    return "deny"
                if msg == "term":
                    return "term"  # 「🖥️ 终端查看」:退回终端审批
                # 其它内容忽略,继续等
        finally:
            try:
                resp.close()
            except Exception:
                pass
    return None


def _dump_topic(reply_topic):
    """调试留痕(需 WATCH_DEBUG_DUMP=1):记下本次回执 topic。出问题时可去 ntfy 拉这个
    topic 的历史(GET /<topic>/json?poll=1&since=...),核对按钮实际发了什么。"""
    if os.environ.get("WATCH_DEBUG_DUMP", "").strip() == "1":
        try:
            import tempfile
            with open(os.path.join(
                tempfile.gettempdir(), "watch_approve_last_topic_%s.txt" % AGENT
            ), "w") as _tf:
                _tf.write(reply_topic)
        except Exception:
            pass


def answered_input(tool_input, q, i):
    """官方机制(hooks 文档):AskUserQuestion 被 PreToolUse hook 拦下后,返回
    allow + updatedInput——原样保留 questions,外加 answers={问题原文: 所选 label 原文},
    Claude Code 即视为「问题已被回答」:终端不再弹选项框,直接按该答案继续。"""
    ui = dict(tool_input) if isinstance(tool_input, dict) else {}
    ui["answers"] = {q["question_raw"]: q["labels_raw"][i]}
    return ui


def handle_question(data, tool_input):
    """AskUserQuestion(终端多选决策框)的手表分支。不返回,内部必 emit。

    点「方案X」-> allow + updatedInput 回填 answers(见 answered_input):终端不再弹框,
    Claude 直接按该选项继续。
    点「在终端查看」/ 超时 / 推送失败 / 配置缺失 / 题型复杂(多问题、多选)-> allow 放行
    (不带 updatedInput),题目照常在终端弹出 —— 对这个工具 allow 永远无害,
    它只是让终端选项框正常出现。
    """
    if not PUSHCUT_KEY or not NTFY_TOPIC:
        emit("allow", "watch-approve: 缺少 PUSHCUT_KEY 或 NTFY_TOPIC,选择题改在终端弹出。")

    q = parse_question(tool_input)
    folder = cwd_label(data) if SHOW_CWD else ""
    # \u00a0 = 不换行空格：手表屏窄，📁 后接普通空格会被折成「emoji 单独一行」。
    suffix = ("\n📁\u00a0" + folder) if folder else ""
    opener = make_opener()

    # 复杂题型(多问题 / 多选)或没开动态按钮:手表只提醒一声,选择回终端做。
    if q is None or not DYNAMIC_ACTIONS:
        first = ""
        try:
            first = " ".join(str(tool_input["questions"][0]["question"]).split())
        except Exception:
            pass
        if len(first) > DESC_MAX:
            first = first[: DESC_MAX - 1] + "…"
        try:
            send_pushcut(
                opener, QUESTION_TITLE,
                "🖥️ 选项较复杂,请到终端选择" + (("\n" + first) if first else "") + suffix,
                with_actions=False, retries=5, sound=QUESTION_SOUND,
            )
        except Exception:
            pass
        emit("allow", "watch-approve: 选择题型较复杂(多问题/多选),已提醒手表,请在终端选择。")

    # 单问题单选:推「方案A/B/.../在终端查看」按钮,等回执。
    reply_topic = make_reply_topic()
    _dump_topic(reply_topic)
    t0 = int(time.time())
    deadline = time.monotonic() + APPROVE_WAIT
    try:
        send_pushcut(
            opener, QUESTION_TITLE, question_body(q) + suffix,
            reply_topic=reply_topic, sound=QUESTION_SOUND,
            actions=build_question_actions(reply_topic, len(q["labels"])),
        )
    except Exception:
        emit("allow", "watch-approve: 选择题推送手表失败,改在终端弹出。")

    tokens = set("opt" + c for c in _OPT_LETTERS.lower()[: len(q["labels"])]) | {"term"}
    try:
        choice = wait_for_decision(opener, t0, deadline, reply_topic, tokens=tokens)
    except Exception:
        choice = None

    if choice and choice.startswith("opt"):
        i = _OPT_LETTERS.lower().index(choice[3])
        emit(
            "allow",
            "watch-approve: 用户已在手表上选择 方案%s「%s」,答案已通过 updatedInput 回填。"
            % (_OPT_LETTERS[i], q["labels"][i]),
            updated_input=answered_input(tool_input, q, i),
        )
    emit(
        "allow",
        "watch-approve: %s,选择题改在终端弹出。"
        % ("已在手表上选择「在终端查看」" if choice == "term"
           else "%ss 内手表无回应" % APPROVE_WAIT),
    )


def main():
    global HOOK_EVENT
    # 1) 读 stdin JSON;解析失败 -> ask 退化
    #    以二进制读 + utf-8-sig 解码,顺手吃掉可能的 BOM(某些 shell 管道会加),
    #    再剥掉前后空白,最大化兼容性。
    try:
        raw_bytes = sys.stdin.buffer.read()
        raw = raw_bytes.decode("utf-8-sig", "replace").strip()
        data = json.loads(raw) if raw else {}
    except Exception:
        emit("ask", "watch-approve: 无法解析 hook 输入,退回正常审批。")

    # 调试留痕(需 WATCH_DEBUG_DUMP=1):本次 hook 的原始输入覆盖写到
    # %TEMP%/watch_approve_last_input_<agent>.json,排查「hook 触发没有/传了什么」直接看文件。
    if os.environ.get("WATCH_DEBUG_DUMP", "").strip() == "1":
        try:
            import tempfile
            with open(os.path.join(tempfile.gettempdir(), "watch_approve_last_input_%s.json" % AGENT), "wb") as _f:
                _f.write(("agent=%s\n" % AGENT).encode("utf-8") + raw_bytes)
        except Exception:
            pass
    if not isinstance(data, dict):
        data = {}

    tool_name = data.get("tool_name", "Tool")
    tool_input = data.get("tool_input", {})
    HOOK_EVENT = str(data.get("hook_event_name") or "")

    # 1.5) AskUserQuestion(Claude 的终端多选决策框):专门分支,上手表直接选。
    #      必须放在 danger-only 之前——它没有 command/file_path,落到后面会被当
    #      「非危险」静默放行,题目只在终端干等。Codex 没有这个工具,不会进来。
    if ASK_QUESTIONS and tool_name == "AskUserQuestion" and HOOK_EVENT != "PermissionRequest":
        handle_question(data, tool_input)  # 内部必 emit,不会返回

    # 2) 关键配置缺失 -> ask 退化(不报错卡住)
    if not PUSHCUT_KEY or not NTFY_TOPIC:
        emit("ask", "watch-approve: 缺少 PUSHCUT_KEY 或 NTFY_TOPIC,退回正常审批。")

    desc = describe(tool_name, tool_input)

    # Codex 的 PermissionRequest:只在 Codex 自己判定「这事需要人批准」(提权/出沙箱/联网等)
    # 时才触发,频率低且都是真正的权限边界 -> 不做危险过滤,一律上手表让你点。
    # 正文优先用 Codex 给的人话描述(tool_input.description),没有再用 describe() 的结果。
    is_perm_request = HOOK_EVENT == "PermissionRequest"
    if is_perm_request and isinstance(tool_input, dict):
        _d = " ".join(str(tool_input.get("description") or "").split())
        if _d:
            desc = _d if len(_d) <= DESC_MAX else _d[: DESC_MAX - 1] + "…"

    # 多窗口并行:正文末尾带「📁 项目文件夹名」,手表上一眼分清是哪个窗口在说话。
    if SHOW_CWD:
        _folder = cwd_label(data)
        if _folder:
            desc = (desc + "\n📁\u00a0" + _folder) if desc else "📁\u00a0" + _folder

    # 0) 会被 Claude Code 强制弹终端的操作(改 .claude/ 里的 settings 等):它的终端确认
    #    hook 压不住(issue #41615),所以这里**不做手表审批、不等回执**,只给手表/手机推一条
    #    无按钮的「去终端确认」提醒,然后返回 ask 让终端照常弹。你就不会对着静默终端发呆。
    #    重试数压到 5(默认 12 会拖慢终端弹窗出现);推送失败也吞掉,绝不卡住。
    if (not is_perm_request) and is_terminal_forced(tool_name, tool_input):
        try:
            send_pushcut(make_opener(), "⚠️ 需去终端确认", desc, with_actions=False, retries=5)
        except Exception:
            pass
        emit("ask", "watch-approve: 该操作会被 Claude Code 强制要求终端确认,已推提醒到手表/手机。")

    # danger-only 模式:不危险的操作按 NONDANGER_DECISION 处理(不打扰手表)。
    # 注意:matcher 现在是 "*"(命中所有工具,见 settings.json),所以这里只把
    # **真正能造成破坏的字段**(shell 命令 / 写入的文件路径)拿去做危险匹配;
    # 像 Read/Glob/Grep/WebSearch 这类没有 command/file_path 的工具,match_text 为空
    # -> 判为非危险 -> 直接放行。**绝不**再去扫 desc(整段 JSON),否则搜索词里
    # 出现 "rm -" 之类会误判成危险、白白震你手表。
    if DANGER_ONLY and not is_perm_request:
        match_text = ""
        if isinstance(tool_input, dict):
            match_text = str(
                tool_input.get("command")
                or tool_input.get("file_path")
                or tool_input.get("notebook_path")
                or ""
            )
        if not is_dangerous(match_text) and not is_protected_write(
            tool_name, tool_input, str(data.get("cwd") or "")
        ):
            if NONDANGER_DECISION == "allow":
                emit("allow", "watch-approve: 非危险操作,自动放行(未打扰手表)。")
            elif NONDANGER_DECISION == "deny":
                emit("deny", "watch-approve: 非危险操作,按配置拒绝。")
            else:
                emit("ask", "watch-approve: 非危险操作,退回正常审批(未打扰手表)。")

    opener = make_opener()

    # 3) 防竞态:先记 t0,再发通知,订阅用 since=t0。
    #    回执 topic 本次审批专属(多窗口互不串台),按钮和订阅用同一个。
    reply_topic = make_reply_topic()
    _dump_topic(reply_topic)
    t0 = int(time.time())
    deadline = time.monotonic() + APPROVE_WAIT

    title = _PRESET["title"]
    text = desc if desc else "(无详情)"
    try:
        send_pushcut(opener, title, text, reply_topic=reply_topic)
    except urllib.error.HTTPError as e:
        hint = ""
        if e.code == 404:
            hint = "(通知 '%s' 不存在?去 Pushcut app 建一条同名通知)" % PUSHCUT_NOTIF
        elif e.code in (401, 403):
            hint = "(PUSHCUT_KEY 无效?)"
        emit("ask", "watch-approve: Pushcut 返回 HTTP %s%s,退回正常审批。" % (e.code, hint))
    except Exception as e:
        emit(
            "ask",
            "watch-approve: 推送 Pushcut 失败(%s,可能是代理/网络),退回正常审批。"
            % type(e).__name__,
        )

    # 4) 等手表回执(只听本次审批的回执 topic)
    try:
        decision = wait_for_decision(opener, t0, deadline, reply_topic)
    except Exception:
        decision = None

    if decision == "allow":
        emit("allow", "watch-approve: 已在手表上批准。")
    elif decision == "deny":
        emit("deny", "watch-approve: 已在手表上拒绝。")
    elif decision == "term":
        emit("ask", "watch-approve: 已在手表上选择「终端查看」,退回终端审批。")
    else:
        emit(
            TIMEOUT_DECISION,
            "watch-approve: %ss 内无回应,按超时策略返回 %s。"
            % (APPROVE_WAIT, TIMEOUT_DECISION),
        )


# ---------- CLI:--doctor 自检 / --print-*-config 配置生成(人用的,不是 hook 路径) ----------
def _say(msg, err=False):
    """诊断/配置输出(给人看的文本,不是 hook JSON)。Windows 控制台可能是 GBK,
    编不出去的字符替换掉,绝不因打印崩溃。"""
    stream = sys.stderr if err else sys.stdout
    try:
        stream.write(msg + "\n")
    except Exception:
        enc = getattr(stream, "encoding", None) or "utf-8"
        try:
            stream.write(msg.encode(enc, "replace").decode(enc, "replace") + "\n")
        except Exception:
            pass
    try:
        stream.flush()
    except Exception:
        pass


def _mask_secret(s):
    return ("****" + s[-4:]) if len(s) > 4 else "****"


def _doctor_get_json(opener, url, timeout=10):
    req = urllib.request.Request(url, headers={"API-Key": PUSHCUT_KEY})
    with opener.open(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


def run_doctor():
    """`python watch_approve.py --doctor`:把 README 排错表自动跑一遍。

    逐项检查 Python / 配置 / Pushcut(key、设备名、通知名)/ ntfy 收发回环,
    最后真发一条测试通知。只读自测,不写任何配置。返回退出码:0=全过,1=有失败项。
    """
    fails = [0]

    def chk(level, text):
        if level == "FAIL":
            fails[0] += 1
        _say("[%s] %s" % (level, text))

    _say("== watch-approve 自检 (agent=%s) ==" % AGENT)

    v = sys.version_info
    chk("OK" if v >= (3, 6) else "FAIL", "Python %d.%d.%d" % (v[0], v[1], v[2]))

    if _ENV_FILE_LOADED:
        chk("OK", "配置文件已读取:%s" % _ENV_FILE_PATH)
    else:
        chk("WARN", "未读到 watch.env(找过:%s)——只用进程环境变量" % _ENV_FILE_PATH)

    if not PUSHCUT_KEY:
        chk("FAIL", "PUSHCUT_KEY 缺失(Pushcut app -> Account -> API 获取)")
    elif "REPLACE_WITH" in PUSHCUT_KEY.upper():
        chk("FAIL", "PUSHCUT_KEY 还是示例占位符,没填真实 key")
    else:
        chk("OK", "PUSHCUT_KEY 已配置(%s)" % _mask_secret(PUSHCUT_KEY))

    if not NTFY_TOPIC:
        chk("FAIL", "NTFY_TOPIC 缺失(回执通道;topic 名就是密码,取长随机串)")
    elif "REPLACE_WITH" in NTFY_TOPIC.upper():
        chk("FAIL", "NTFY_TOPIC 还是示例占位符,没填真实 topic")
    elif len(NTFY_TOPIC) < 12:
        chk("WARN", "NTFY_TOPIC 只有 %d 字符:topic 名就是密码,建议 16+ 位随机串" % len(NTFY_TOPIC))
    else:
        chk("OK", "NTFY_TOPIC 已配置(%s)" % _mask_secret(NTFY_TOPIC))

    chk("OK", "PUSHCUT_NOTIF=%s" % PUSHCUT_NOTIF)
    if PROXY:
        chk("OK", "代理:%s" % PROXY)
    else:
        chk("WARN", "未配置代理,直连(国内连不上 Pushcut/ntfy 时配 HTTPS_PROXY)")

    if APPROVE_WAIT >= 300:
        chk("WARN", "APPROVE_WAIT=%ds >= hook timeout(示例配置 300s):宿主会先掐掉 hook,请调小" % APPROVE_WAIT)
    else:
        chk("OK", "APPROVE_WAIT=%ds(小于示例 hook timeout 300s)" % APPROVE_WAIT)

    chk("OK", "模式:danger_only=%s nondanger=%s 动态按钮=%s 独立回执topic=%s 终端查看按钮=%s"
        % (DANGER_ONLY, NONDANGER_DECISION, DYNAMIC_ACTIONS, UNIQUE_TOPIC, TERMINAL_BUTTON))
    if _SELF_DIR:
        chk("OK", "脚本自身目录已受保护:%s(WATCH_PROTECT_SELF=0 可关)" % _SELF_DIR)
    else:
        chk("WARN", "未保护脚本自身目录(WATCH_PROTECT_SELF=0):agent 可以改掉这套 hook")
    if PROTECT_PATHS:
        chk("OK", "额外受保护路径子串:%s" % ",".join(PROTECT_PATHS))

    if not PUSHCUT_KEY or not NTFY_TOPIC:
        _say("-- 关键配置缺失,跳过网络检查 --")
        _say("== 结果:%d 项失败,按上面 [FAIL] 逐条补齐 ==" % fails[0])
        return 1

    opener = make_opener()

    # Pushcut:key 是否有效 + 账号真实设备名(PUSHCUT_DEVICES 名字不对,发通知会 400)
    device_names = []
    try:
        devs = _doctor_get_json(opener, "https://api.pushcut.io/v1/devices")
        for d in devs if isinstance(devs, list) else []:
            n = (d.get("id") or d.get("name")) if isinstance(d, dict) else None
            device_names.append(str(n if n else d))
        chk("OK", "Pushcut API 连通,账号设备:%s" % (", ".join(device_names) or "(无)"))
    except urllib.error.HTTPError as e:
        chk("FAIL", "Pushcut API 返回 HTTP %d%s"
            % (e.code, "(PUSHCUT_KEY 无效?)" if e.code in (401, 403) else ""))
    except Exception as e:
        chk("FAIL", "连不上 Pushcut API(%s):检查网络 / HTTPS_PROXY" % type(e).__name__)

    if PUSHCUT_DEVICES and device_names:
        lowered = [n.lower() for n in device_names]
        unknown = [d for d in PUSHCUT_DEVICES if d.lower() not in lowered]
        if unknown:
            chk("FAIL", "PUSHCUT_DEVICES 里这些设备名账号里没有:%s(发通知会 400;改成上面列出的真名)"
                % ",".join(unknown))
        else:
            chk("OK", "PUSHCUT_DEVICES=%s 全部匹配" % ",".join(PUSHCUT_DEVICES))

    # Pushcut:云端有没有名为 PUSHCUT_NOTIF 的通知(没有,发通知会 404)
    try:
        notifs = _doctor_get_json(opener, "https://api.pushcut.io/v1/notifications")
        names = []
        for nobj in notifs if isinstance(notifs, list) else []:
            if isinstance(nobj, dict):
                names.extend(str(x) for x in (nobj.get("id"), nobj.get("title")) if x)
        if PUSHCUT_NOTIF in names:
            chk("OK", "Pushcut 云端存在通知「%s」" % PUSHCUT_NOTIF)
        elif names:
            chk("FAIL", "Pushcut 云端没有名为「%s」的通知(现有:%s)。去 app 里建一条并等同步"
                % (PUSHCUT_NOTIF, ", ".join(sorted(set(names)))))
        else:
            chk("WARN", "通知列表为空或格式未知,跳过此项(若发通知 404 = 名字不对)")
    except Exception as e:
        chk("WARN", "列举 Pushcut 通知失败(%s),跳过此项" % type(e).__name__)

    # ntfy:publish -> poll 回环(doctor 专属临时 topic,不打扰真实通道)
    topic = NTFY_TOPIC + "-doctor-" + os.urandom(4).hex()
    t0 = int(time.time()) - 2
    pub_ok = False
    try:
        with opener.open(
            NTFY_BASE + urllib.parse.quote(topic, safe="") + "/publish?message=ping", timeout=10
        ) as r:
            r.read()
        pub_ok = True
    except Exception as e:
        chk("FAIL", "ntfy publish 失败(%s @ %s):检查网络 / NTFY_BASE / 代理"
            % (type(e).__name__, NTFY_BASE))
    if pub_ok:
        got = False
        for _ in range(3):
            try:
                with opener.open(
                    NTFY_BASE + urllib.parse.quote(topic, safe="") + "/json?poll=1&since=" + str(t0),
                    timeout=10,
                ) as r:
                    for ln in r.read().decode("utf-8", "replace").splitlines():
                        try:
                            o = json.loads(ln)
                        except Exception:
                            continue
                        if o.get("event") == "message" and (o.get("message") or "").strip() == "ping":
                            got = True
                            break
            except Exception:
                pass
            if got:
                break
            time.sleep(1)
        if got:
            chk("OK", "ntfy 收发回环正常(%s)" % NTFY_BASE)
        else:
            chk("FAIL", "ntfy publish 成功但订阅读不回(%s):服务端不通或被墙" % NTFY_BASE)

    # 压轴:真发一条测试通知(无按钮)。注意 API「成功」!= 设备收到(token 失效照样返回成功)。
    try:
        send_pushcut(opener, "🩺 watch-approve 自检", "看到这条 = Pushcut→设备链路 OK",
                     with_actions=False, retries=3)
        chk("OK", "测试通知已发出 -> 看下 iPhone/手表。没收到 = 推送 token 失效,重开 iPhone 上的 Pushcut app")
    except urllib.error.HTTPError as e:
        extra = ""
        if e.code == 404:
            extra = "(云端没有通知「%s」)" % PUSHCUT_NOTIF
        elif e.code in (401, 403):
            extra = "(PUSHCUT_KEY 无效)"
        elif e.code == 400:
            extra = "(请求被拒,常见原因:PUSHCUT_DEVICES 设备名不对)"
        chk("FAIL", "发测试通知失败:HTTP %d%s" % (e.code, extra))
    except Exception as e:
        chk("FAIL", "发测试通知失败(%s):网络/代理问题" % type(e).__name__)

    if fails[0] == 0:
        _say("== 结果:全部通过 ==")
        return 0
    _say("== 结果:%d 项失败,按上面 [FAIL] 逐条排查 ==" % fails[0])
    return 1


def print_config(argv):
    """`--print-claude-config` / `--print-codex-config`(或 `--print-config` 两份都给):
    打印可直接粘贴的 hooks 配置片段。JSON 走 stdout(可重定向),提示走 stderr。
    脚本路径取当前文件所在目录的绝对路径,由 json.dumps 转义——Windows 反斜杠 /
    引号这些最容易抄错的地方都不用手改。"""
    here = os.path.dirname(os.path.abspath(__file__))

    def cmd(script, agent=None):
        c = 'python "%s"' % os.path.join(here, script)
        return c + ((" --agent " + agent) if agent else "")

    both = "--print-config" in argv
    if both or "--print-claude-config" in argv:
        cfg = {
            "hooks": {
                "PreToolUse": [{"matcher": "*", "hooks": [
                    {"type": "command", "command": cmd("watch_approve.py"), "timeout": 300}]}],
                "Stop": [{"hooks": [
                    {"type": "command", "command": cmd("watch_done.py"), "timeout": 60}]}],
                "StopFailure": [{"hooks": [
                    {"type": "command", "command": cmd("watch_done.py"), "timeout": 60}]}],
            }
        }
        _say("# 合并进 ~/.claude/settings.json(或项目 .claude/settings.json),重启 Claude Code;"
             "密钥放脚本同目录的 watch.env:", err=True)
        _say(json.dumps(cfg, ensure_ascii=False, indent=2))
    if both or "--print-codex-config" in argv:
        cfg = {
            "hooks": {
                "PermissionRequest": [{"matcher": "*", "hooks": [
                    {"type": "command", "command": cmd("watch_approve.py", "codex"),
                     "statusMessage": "Waiting for watch approval", "timeout": 300}]}],
                "Stop": [{"hooks": [
                    {"type": "command", "command": cmd("watch_done.py", "codex"), "timeout": 60}]}],
            }
        }
        _say("# 存为 ~/.codex/hooks.json;改完在 Codex TUI 里跑 /hooks 重新信任这两条 hook:", err=True)
        _say(json.dumps(cfg, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    _argv = sys.argv[1:]
    if "--doctor" in _argv:
        sys.exit(run_doctor())
    if any(a in _argv for a in ("--print-config", "--print-claude-config", "--print-codex-config")):
        sys.exit(print_config(_argv))
    try:
        main()
    except SystemExit:
        raise
    except Exception as e:
        # 兜底:任何意外都 fail-safe 成 ask,绝不崩溃
        try:
            emit("ask", "watch-approve: 未知异常(%s),退回正常审批。" % type(e).__name__)
        except Exception:
            sys.exit(0)
