# watch-approve

在 **Apple Watch** 上批准你的编码 agent 的操作。

当 **Claude Code** 或 **Codex CLI** 要执行 shell 命令或改文件时,这个 hook 会把请求推到你的
iPhone / Apple Watch,你在表上点 **Allow / Deny**,结果回传决定是否放行——不用盯着终端。

> English docs: [README.md](./README.md)

```
PreToolUse hook
  → POST 通知到 Pushcut(带 Allow/Deny 两个「后台 web 请求」按钮)
  → 以「限时通知」发出,iPhone 在用时也能上 Apple Watch,你在表上点 Allow / Deny
  → Pushcut 执行按钮的后台 web 请求 → publish 到 ntfy 的 topic(allow/deny)
  → hook 从 ntfy stream 读到结果
  → 返回 permissionDecision(allow / deny / ask)给 agent
```

单文件、零依赖(`watch_approve.py`,只用 Python 标准库),并且**全程 fail-safe**:任何配置缺失 /
网络错误 / 超时,都退回 agent 自己的终端审批弹窗,绝不崩溃、绝不卡死。

---

## 适用场景
- agent 跑长任务时你在玩手机 / 离开键盘。
- 想给危险操作(`rm -rf`、`git push --force`、上生产)加一道人工确认,又不想守着终端。
- 支持走代理(按本地 Clash HTTP 代理设计/实测),在直连 Pushcut/ntfy 不稳的网络也能用。

## 原理
- **[Pushcut](https://www.pushcut.io/)** —— 发带按钮的推送通知。由 hook 动态注入按钮需要 **Pushcut Pro**。
- **[ntfy](https://ntfy.sh/)** —— 一个 pub/sub topic 当回传通道。公共 `ntfy.sh` 即可;**topic 名当密码**,要长要随机。

按钮是「**后台 web 请求**」(不是「打开网址」),因为 watchOS 的通知动作只支持后台 web 请求,
不支持「打开 App / 跑快捷指令」。它用 GET 方式 publish 到 `https://ntfy.sh/<topic>/publish?message=allow`(或 `deny`)。

## 前置条件
- **Claude Code** 和/或 **Codex CLI**(两者都有同契约的 `PreToolUse` hook)。
- **Python 3** 在 PATH(只用标准库,无需 pip 安装)。
- **Pushcut** 账户(动态注入按钮需 **Pro**)。
- **Apple Watch 上装了 Pushcut app**(表才能收到+操作)。
- 一个 **ntfy** topic(公共 `ntfy.sh` 即可)。

---

## 配置步骤

### 1. Pushcut
1. 注册,iPhone **和 Apple Watch** 都装上 app。
2. 建一条 Notification(名字填进 `PUSHCUT_NOTIF`,如 `claude`)。标题/正文留空——hook 会动态覆盖。
   用默认的动态按钮就**不用**手动加按钮。
3. 在 **Account → API** 拿 API key。

### 2. ntfy
取一个长随机 topic 名,如 `myagent_8f3k2j9x`。不用安装,选个名字就行。

### 3. 安装 hook
把 `watch_approve.py` 放到一个固定位置,让 agent 指向它。

**Claude Code** —— 把 [`examples/claude/settings.example.json`](./examples/claude/settings.example.json)
合并进 `.claude/settings.json`(项目)或 `~/.claude/settings.json`(全局),填好 env 和脚本绝对路径。

**Codex CLI** —— 把 [`examples/codex/hooks.example.json`](./examples/codex/hooks.example.json) 放到
`~/.codex/hooks.json`(或 `<repo>/.codex/hooks.json`)。Codex 不按 hook 注入 env,所以把
`PUSHCUT_KEY / NTFY_TOPIC / HTTPS_PROXY / PUSHCUT_SOUND` 等设成**系统/用户环境变量**;再用 `/hooks`
命令信任该 hook(或一次性 `codex --dangerously-bypass-hook-trust`)。

> Codex 的 `PreToolUse` 覆盖 `Bash`、`apply_patch`(改文件)、MCP;Claude Code 覆盖 `Bash`、`Write`、
> `Edit`、`MultiEdit` 等。两份示例的 matcher 已分别对应。

### 4. 先单测(不经过 agent)
```bash
# 先设好环境变量(见下表),然后:
echo '{"hook_event_name":"PreToolUse","tool_name":"Bash","tool_input":{"command":"echo hello"}}' \
  | python /path/to/watch_approve.py
```
点 **Allow**(默认是「限时通知」,iPhone 不锁屏也能上手表),应立刻得到:
```json
{"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"allow","permissionDecisionReason":"watch-approve: approved on watch."}}
```

---

## 配置项(环境变量)

| 变量 | 默认 | 说明 |
|------|------|------|
| `PUSHCUT_KEY` | — | **必填。** Pushcut API key。 |
| `NTFY_TOPIC` | — | **必填。** 你的隐秘 ntfy topic(当密码,要随机)。 |
| `PUSHCUT_NOTIF` | `claude` | 要触发的 Pushcut 通知名。 |
| `HTTPS_PROXY` | — | 所有出网请求的代理,如 `http://127.0.0.1:7890`,回退到 `HTTP_PROXY`。 |
| `PUSHCUT_SOUND` | `default` | 通知声音。**不带的话手表不震!** `vibrateOnly` 只震不响;`none` 静默。 |
| `PUSHCUT_TIME_SENSITIVE` | `1` | `1`=标记为**限时通知**→ iPhone **在用时也能上 Apple Watch**,并冲破专注/勿扰;`0`=普通通知(只有手机锁屏才上表)。 |
| `WATCH_AGENT_LABEL` | `Agent` | 标题前缀,如 `Claude` → "Claude: Bash"。 |
| `APPROVE_WAIT` | `240` | 等回执的秒数,要 **小于** hook 的 `timeout`(300)。 |
| `APPROVE_TIMEOUT_DECISION` | `ask` | 超时没人理时:`ask` / `allow` / `deny`。 |
| `PUSHCUT_DYNAMIC_ACTIONS` | `1` | `1`=hook 注入按钮(需 Pro);`0`=用 app 里手配的按钮。 |
| `PUSHCUT_DEVICES` | — | 逗号分隔的目标设备名(见 `GET /v1/devices`),空=全部。 |
| `PUSHCUT_RETRIES` | `12` | 触发 Pushcut 的重试次数(应对到 api.pushcut.io 的 TLS 偶发失败)。 |
| `PUSHCUT_TIMEOUT` | `6` | 单次触发 Pushcut 的超时(秒)。 |
| `NTFY_BASE` | `https://ntfy.sh/` | ntfy 服务器地址(自建时改)。 |
| `WATCH_DANGER_ONLY` | `0` | `1`=只有「危险」命令才弹手表,其余直接返回 `ask`(见下)。 |
| `WATCH_DANGER_EXTRA` | — | 追加的危险正则(换行分隔,忽略大小写)。 |
| `WATCH_DANGER_REGEX` | — | 单条正则,**整体替换**内置危险清单。 |
| `WATCH_NONDANGER_DECISION` | `ask` | (danger-only 模式)非危险操作怎么办:`ask`(退回终端)、`allow`(自动放行,不弹手表也不在终端问)、`deny`。 |

---

## 降噪(danger-only 模式)

默认情况下,`matcher` 命中的**每一个**工具调用都会让你审批——可能很烦。把 `WATCH_DANGER_ONLY=1`
打开,hook 就只在**危险**命令时弹手表,其余直接返回 `ask`(agent 照常工作,手表保持安静)。

推荐的低打扰配置:`WATCH_DANGER_ONLY=1` + 把 `matcher` 收窄成 `"Bash"`。(收窄 matcher 很安静,但它没列到的
工具会退回终端——想完全不碰终端见下面的**自动驾驶**。)

内置危险清单会命中:`rm -rf`、`sudo`、`git push --force`、`git reset --hard`、`dd`、`mkfs`、
`chmod 777`、`shutdown`/`reboot`、`kill`、`drop/truncate table`、`delete from`、`curl ... | sh`、
`docker prune`、`terraform destroy`、`kubectl delete`、PowerShell 的 `Remove-Item -Recurse -Force` 等。
可用 `WATCH_DANGER_EXTRA` 追加,或用 `WATCH_DANGER_REGEX` 整体替换。

> 开了 danger-only 后,要用危险命令测试(如 `rm -rf /tmp/x`)——普通的 `echo hello` 按设计**不会**弹通知。

**自动驾驶 + 安全网。** 默认 danger-only 下,非危险操作走 `ask`(终端可能还会问)。如果你**根本不想碰终端**
——"只有危险操作在手表上拦,其余全自动跑"——就把 **`"matcher": "*"`** 配上 `WATCH_DANGER_ONLY=1` 和
`WATCH_NONDANGER_DECISION=allow`。这样**每个**工具都过 hook:非危险的(读文件、联网搜索、MCP 调用……)
静默自动放行,只有命中危险清单的才弹手表。

> **为什么用 `"matcher": "*"` 而不是收窄?** matcher 是白名单——它**没列到**的工具(`WebSearch`、MCP 工具、
> 以后新增的工具……)会**完全跳过 hook**,退回 agent 的**终端**弹窗,永远到不了你的手表/手机。白名单永远会
> 漏掉下一个新工具,所以想彻底不碰终端,就匹配**全部**工具、交给危险过滤器判断。(Claude Code 文档:`"*"`、
> `""` 或省略 matcher 都表示「匹配所有工具」。)

> ⚠️ 两点注意:(1) `"matcher": "*"` 必须配 `WATCH_DANGER_ONLY=1`——`"*"` 不配 danger-only 会让**每一个**工具
> 调用都弹手表(连每次读文件都震)。(2) 用 `WATCH_NONDANGER_DECISION=allow` 时,危险清单**没**逮到的操作会
> **无确认直接执行**;危险正则就是你唯一的关卡,按需扩充(`WATCH_DANGER_EXTRA`)。

---

## Apple Watch 注意事项(重要)
- **靠「限时通知」才能稳定上手表。** 默认情况下,*普通*通知只有 iPhone **锁屏/息屏**时才上 Apple Watch;
  手机解锁在用时 iOS 会把通知留在手机(Apple 的路由机制,不是 bug)。把通知标记为**限时**
  (`PUSHCUT_TIME_SENSITIVE=1`,默认开)就能冲破这条规则,**手机正在用时也能上手表**,并冲破专注/勿扰。
  设备实测 A/B:两条只差这个字段的通知,手机在用时**只有限时那条到了手表**。(需在 iOS 设置 → 通知里允许
  Pushcut 的「限时通知」——默认允许。)
- **手表要装 Pushcut app**,否则表上无法操作。
- **按钮必须是后台 web 请求**(本 hook 已是)。否则 watchOS 报「actions that run shortcuts or open
  apps are not supported on watchOS」。
- **要带声音**(`PUSHCUT_SOUND=default`),否则手表不震。
- **某台设备突然收不到了,就在它上面重开 Pushcut app。** app 被杀 / 长时间后台后,推送 token 会失效;此时 Pushcut 仍一直返回「成功」,但什么都到不了。重开会重新注册 token(并让手表重新同步)。

## 延迟
有几秒的物理地板:发通知要往返 Pushcut,回执要经 ntfy 往返,再加 Apple 推表的耗时。代理慢/不稳的话,
给 `api.pushcut.io` 和 `ntfy.sh` 选个更快的节点最有效。

## 排错(看输出里的 `permissionDecisionReason`)

| 现象 | 原因 / 处理 |
|------|------|
| `Pushcut returned HTTP 404` | Pushcut 云端没有这个 `PUSHCUT_NOTIF` 通知(去建,并确认 app 同步上去了)。 |
| `Pushcut returned HTTP 401/403` | `PUSHCUT_KEY` 不对。 |
| `failed to reach Pushcut (...)` | 代理/网络不通,或 TLS 反复失败(调大 `PUSHCUT_RETRIES`)。 |
| hook 发送不报错,但**所有设备**都收不到 | Pushcut 接口已接收这条推送(返回成功),但 APNs 没投递——多半是**推送 token 失效**(Pushcut app 被系统杀掉 / 长时间后台)。**打开 iPhone 上的 Pushcut app** 重新注册 token(顺带让手表重新同步)。记住:Pushcut 返回「成功」只代表云端**接收**了,不代表设备**收到**了。 |
| 手机震、手表不震 | 保持 `PUSHCUT_TIME_SENSITIVE=1`(默认)让通知是「限时」的,手机在用时也能上手表;并确认手表装了 Pushcut app、iOS 设置 → 通知里允许 Pushcut 的限时通知。 |
| agent 还是在**终端**弹确认(到不了手表/手机) | 那个工具不在你的 `matcher`(白名单)里——如 `WebSearch`、MCP 工具。用 `"matcher": "*"` 配 `WATCH_DANGER_ONLY=1` + `WATCH_NONDANGER_DECISION=allow`,让每个工具都过 hook。 |
| 表上点了提示「not supported」 | 按钮不是后台 web 请求(默认配置已是)。 |
| 不震动 | 设 `PUSHCUT_SOUND=default`(或 `vibrateOnly`)。 |
| 通知文字显示成 `???`(Windows 手动测试) | 是 PowerShell 的锅,不是 hook:用管道把非 ASCII 喂给原生程序时,`$OutputEncoding` 默认 ASCII,会把中文/emoji 压成 `?`。hook 本身读 UTF-8、发 `\uXXXX` 转义的 JSON,agent 实际运行时渲染正常。Windows 上测非 ASCII 时,改用 UTF-8 文件喂 JSON 或用环境变量传值,别用内联管道。 |

任何失败都返回 `ask`,agent 退回正常弹窗,绝不卡死。

## 安全
- 密钥(`PUSHCUT_KEY`、`NTFY_TOPIC`)从环境变量读、不硬编码。别提交真实 `settings.json`——`.gitignore` 已排除。
- 公共 `ntfy.sh` 上,topic 名是回传通道唯一的防线,用长随机值,或自建带鉴权的 ntfy(`NTFY_BASE`)。
- `allow` 会让 agent 跳过自己的权限弹窗。`matcher` 只拦你真正想管的工具。

## 许可证
MIT —— 见 [LICENSE](./LICENSE)。
