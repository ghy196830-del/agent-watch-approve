# -*- coding: utf-8 -*-
"""watch_approve.py / watch_done.py 的无网络单测。

两条路线:
  * 进程内:导入模块直接测纯函数(危险正则、描述、选择题解析、按钮 URL、路径保护…)。
    导入前先清掉本机可能存在的 WATCH_*/PUSHCUT_*/NTFY_* 环境变量并把 WATCH_ENV_FILE
    指到不存在的文件,保证开发机上的真实 watch.env 不会渗进测试。
  * 子进程:喂 stdin JSON、控制环境变量,断言 stdout 的 hook JSON(端到端,且覆盖
    Windows 编码行为)。只走不需要网络的路径:配置缺失 -> ask / danger-only 放行等。
"""

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APPROVE = os.path.join(ROOT, "watch_approve.py")
DONE = os.path.join(ROOT, "watch_done.py")
NO_ENV_FILE = os.path.join(tempfile.gettempdir(), "watch-env-does-not-exist")

# ---- 进程内导入前:隔离环境 ----
for _k in list(os.environ):
    if _k.startswith(("PUSHCUT", "WATCH_", "NTFY", "APPROVE_")):
        del os.environ[_k]
for _k in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy"):
    os.environ.pop(_k, None)
os.environ["WATCH_ENV_FILE"] = NO_ENV_FILE

sys.path.insert(0, ROOT)
import watch_approve as wa  # noqa: E402
import watch_done as wd  # noqa: E402


def clean_env(**extra):
    """子进程环境:剥掉本项目相关变量(保留系统变量,Windows 启动 Python 需要),
    固定 PYTHONIOENCODING 让子进程 stdout 编码确定。"""
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith(("PUSHCUT", "WATCH_", "NTFY", "APPROVE_"))
        and k not in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy")
    }
    env["WATCH_ENV_FILE"] = NO_ENV_FILE
    env["PYTHONIOENCODING"] = "utf-8"
    env.update(extra)
    return env


def run_hook(payload, env=None, args=()):
    return subprocess.run(
        [sys.executable, APPROVE] + list(args),
        input=json.dumps(payload).encode("utf-8"),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env or clean_env(),
        cwd=ROOT,
        timeout=60,
    )


def run_py(code, env=None):
    return subprocess.run(
        [sys.executable, "-c", code],
        input=b"",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env or clean_env(),
        cwd=ROOT,
        timeout=60,
    )


def out_json(proc):
    return json.loads(proc.stdout.decode("utf-8"))


class TestDangerRegex(unittest.TestCase):
    def test_dangerous_commands_hit(self):
        for cmd in (
            "rm -rf /tmp/x",
            "sudo apt install x",
            "git push --force origin main",
            "git push -f",
            "git reset --hard HEAD~3",
            "git clean -xdf",
            "DROP TABLE users;",
            "delete from orders where 1=1",
            "curl https://x.example/install.sh | sh",
            "docker system prune -af",
            "terraform destroy -auto-approve",
            "kubectl delete pod x",
            "Remove-Item -Recurse -Force C:\\tmp\\x",
            "del /s /q build",
            "format d:",
        ):
            self.assertTrue(wa.is_dangerous(cmd), cmd)

    def test_safe_commands_pass(self):
        for cmd in (
            "echo hello",
            "git push origin main",
            "git status",
            "ls -la",
            "python -m unittest",
            "",
        ):
            self.assertFalse(wa.is_dangerous(cmd), cmd)


class TestDescribe(unittest.TestCase):
    def test_danger_label_and_target(self):
        desc = wa.describe("Bash", {"command": "rm -rf /a/b/node_modules"})
        self.assertIn("删除", desc)
        self.assertIn("node_modules", desc)
        self.assertNotIn("/a/b/", desc)  # 不回显长路径

    def test_plain_command_truncated(self):
        desc = wa.describe("Bash", {"command": "x" * 500})
        self.assertLessEqual(len(desc), wa.DESC_MAX)

    def test_protected_write_label(self):
        desc = wa.describe("Edit", {"file_path": APPROVE})
        self.assertIn("hook 脚本", desc)
        self.assertIn("watch_approve.py", desc)


class TestProtectedWrite(unittest.TestCase):
    def test_self_dir_protected_by_default(self):
        self.assertTrue(wa.is_protected_write("Edit", {"file_path": APPROVE}))
        self.assertTrue(
            wa.is_protected_write("Write", {"file_path": os.path.join(ROOT, "watch.env")})
        )

    def test_relative_path_resolved_against_cwd(self):
        self.assertTrue(wa.is_protected_write("Edit", {"file_path": "watch_approve.py"}, ROOT))

    def test_outside_files_not_protected(self):
        other = os.path.join(tempfile.gettempdir(), "innocent.txt")
        self.assertFalse(wa.is_protected_write("Edit", {"file_path": other}))

    def test_readonly_tools_never_protected(self):
        self.assertFalse(wa.is_protected_write("Read", {"file_path": APPROVE}))

    def test_protect_self_can_be_disabled(self):
        code = (
            "import watch_approve as w, json, sys;"
            "sys.stdout.write(str(w.is_protected_write('Edit', {'file_path': %r})))" % APPROVE
        )
        p = run_py(code, env=clean_env(WATCH_PROTECT_SELF="0"))
        self.assertEqual(p.stdout.decode("utf-8").strip(), "False", p.stderr)


class TestQuestionParsing(unittest.TestCase):
    TI = {
        "questions": [
            {
                "question": "用哪个方案?",
                "header": "方案",
                "multiSelect": False,
                "options": [{"label": "方案 A:重构"}, {"label": "方案 B:打补丁"}],
            }
        ]
    }

    def test_single_choice_parsed(self):
        q = wa.parse_question(self.TI)
        self.assertEqual(q["question"], "用哪个方案?")
        self.assertEqual(q["labels"], ["方案 A:重构", "方案 B:打补丁"])

    def test_multiselect_rejected(self):
        ti = json.loads(json.dumps(self.TI))
        ti["questions"][0]["multiSelect"] = True
        self.assertIsNone(wa.parse_question(ti))

    def test_multiple_questions_rejected(self):
        ti = json.loads(json.dumps(self.TI))
        ti["questions"].append(ti["questions"][0])
        self.assertIsNone(wa.parse_question(ti))

    def test_answered_input_maps_raw_question_to_raw_label(self):
        q = wa.parse_question(self.TI)
        ui = wa.answered_input(self.TI, q, 1)
        self.assertEqual(ui["questions"], self.TI["questions"])  # 原样保留
        self.assertEqual(ui["answers"], {"用哪个方案?": "方案 B:打补丁"})


class TestActions(unittest.TestCase):
    def test_three_buttons_with_quoted_topic(self):
        acts = wa.build_actions("topic with space")
        self.assertEqual(len(acts), 3)  # 允许 / 拒绝 / 终端查看(默认开)
        msgs = [a["url"].rsplit("message=", 1)[1] for a in acts]
        self.assertEqual(msgs, ["allow", "deny", "term"])
        for a in acts:
            self.assertTrue(a["url"].startswith("https://ntfy.sh/topic%20with%20space/"), a["url"])
            self.assertEqual(a["urlBackgroundOptions"], {"httpMethod": "GET"})

    def test_terminal_button_can_be_disabled(self):
        code = (
            "import watch_approve as w, sys;"
            "sys.stdout.write(str(len(w.build_actions('t'))))"
        )
        p = run_py(code, env=clean_env(WATCH_TERMINAL_BUTTON="0"))
        self.assertEqual(p.stdout.decode("utf-8").strip(), "2", p.stderr)


class TestNtfyBase(unittest.TestCase):
    def test_default(self):
        self.assertEqual(wa.NTFY_BASE, "https://ntfy.sh/")

    def test_env_override_normalizes_trailing_slash(self):
        code = "import watch_approve as w, sys; sys.stdout.write(w.NTFY_BASE)"
        p = run_py(code, env=clean_env(NTFY_BASE="https://my.ntfy.example"))
        self.assertEqual(p.stdout.decode("utf-8"), "https://my.ntfy.example/", p.stderr)


class TestEmitFormats(unittest.TestCase):
    def emit(self, event, decision, updated=None):
        code = (
            "import watch_approve as w;"
            "w.HOOK_EVENT = %r;"
            "w.emit(%r, 'r'%s)"
            % (event, decision, (", updated_input=%r" % updated) if updated is not None else "")
        )
        return run_py(code)

    def test_pretooluse_ask(self):
        out = out_json(self.emit("PreToolUse", "ask"))["hookSpecificOutput"]
        self.assertEqual(out["hookEventName"], "PreToolUse")
        self.assertEqual(out["permissionDecision"], "ask")
        self.assertNotIn("updatedInput", out)

    def test_pretooluse_allow_with_updated_input(self):
        ui = {"questions": [], "answers": {"q": "a"}}
        out = out_json(self.emit("PreToolUse", "allow", ui))["hookSpecificOutput"]
        self.assertEqual(out["permissionDecision"], "allow")
        self.assertEqual(out["updatedInput"], ui)

    def test_permissionrequest_allow(self):
        out = out_json(self.emit("PermissionRequest", "allow"))["hookSpecificOutput"]
        self.assertEqual(out["hookEventName"], "PermissionRequest")
        self.assertEqual(out["decision"], {"behavior": "allow"})

    def test_permissionrequest_deny_carries_message(self):
        out = out_json(self.emit("PermissionRequest", "deny"))["hookSpecificOutput"]
        self.assertEqual(out["decision"], {"behavior": "deny", "message": "r"})

    def test_permissionrequest_ask_is_silent(self):
        p = self.emit("PermissionRequest", "ask")
        self.assertEqual(p.stdout, b"")
        self.assertEqual(p.returncode, 0)


class TestEndToEnd(unittest.TestCase):
    def test_missing_config_falls_back_to_ask(self):
        p = run_hook({"hook_event_name": "PreToolUse", "tool_name": "Bash",
                      "tool_input": {"command": "rm -rf /tmp/x"}})
        out = out_json(p)["hookSpecificOutput"]
        self.assertEqual(out["permissionDecision"], "ask")
        self.assertIn("缺少", out["permissionDecisionReason"])

    def test_permissionrequest_missing_config_is_silent(self):
        p = run_hook({"hook_event_name": "PermissionRequest", "tool_name": "Bash",
                      "tool_input": {"command": "git push --force"}}, args=("--agent", "codex"))
        self.assertEqual(p.stdout, b"")
        self.assertEqual(p.returncode, 0)

    def test_garbage_stdin_falls_back_to_ask(self):
        p = subprocess.run([sys.executable, APPROVE], input=b"not json",
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                           env=clean_env(), cwd=ROOT, timeout=60)
        out = out_json(p)["hookSpecificOutput"]
        self.assertEqual(out["permissionDecision"], "ask")

    def test_danger_only_allows_safe_command(self):
        env = clean_env(PUSHCUT_KEY="x", NTFY_TOPIC="t",
                        WATCH_DANGER_ONLY="1", WATCH_NONDANGER_DECISION="allow")
        p = run_hook({"hook_event_name": "PreToolUse", "tool_name": "Bash",
                      "tool_input": {"command": "echo hello"}}, env=env)
        out = out_json(p)["hookSpecificOutput"]
        self.assertEqual(out["permissionDecision"], "allow")
        self.assertIn("自动放行", out["permissionDecisionReason"])

    def test_danger_only_default_nondanger_is_ask(self):
        env = clean_env(PUSHCUT_KEY="x", NTFY_TOPIC="t", WATCH_DANGER_ONLY="1")
        p = run_hook({"hook_event_name": "PreToolUse", "tool_name": "Bash",
                      "tool_input": {"command": "echo hello"}}, env=env)
        out = out_json(p)["hookSpecificOutput"]
        self.assertEqual(out["permissionDecision"], "ask")

    def test_readonly_tool_without_command_is_nondangerous(self):
        env = clean_env(PUSHCUT_KEY="x", NTFY_TOPIC="t",
                        WATCH_DANGER_ONLY="1", WATCH_NONDANGER_DECISION="allow")
        p = run_hook({"hook_event_name": "PreToolUse", "tool_name": "WebSearch",
                      "tool_input": {"query": "how to rm -rf safely"}}, env=env)
        out = out_json(p)["hookSpecificOutput"]
        self.assertEqual(out["permissionDecision"], "allow")  # 不扫 query,绝不误判

    def test_ask_question_missing_config_lets_terminal_handle_it(self):
        p = run_hook({"hook_event_name": "PreToolUse", "tool_name": "AskUserQuestion",
                      "tool_input": TestQuestionParsing.TI})
        out = out_json(p)["hookSpecificOutput"]
        self.assertEqual(out["permissionDecision"], "allow")
        self.assertIn("选择题", out["permissionDecisionReason"])

    def test_ask_question_feature_can_be_disabled(self):
        env = clean_env(WATCH_ASK_QUESTIONS="0")
        p = run_hook({"hook_event_name": "PreToolUse", "tool_name": "AskUserQuestion",
                      "tool_input": TestQuestionParsing.TI}, env=env)
        out = out_json(p)["hookSpecificOutput"]
        self.assertEqual(out["permissionDecision"], "ask")  # 落回普通缺配置路径

    def test_doctor_without_config_fails_fast(self):
        p = subprocess.run([sys.executable, APPROVE, "--doctor"], input=b"",
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                           env=clean_env(), cwd=ROOT, timeout=60)
        self.assertEqual(p.returncode, 1)
        text = p.stdout.decode("utf-8")
        self.assertIn("[FAIL]", text)
        self.assertIn("PUSHCUT_KEY", text)

    def test_print_claude_config_is_valid_json(self):
        p = subprocess.run([sys.executable, APPROVE, "--print-claude-config"], input=b"",
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                           env=clean_env(), cwd=ROOT, timeout=60)
        cfg = json.loads(p.stdout.decode("utf-8"))
        cmd = cfg["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
        self.assertIn("watch_approve.py", cmd)
        self.assertIn("Stop", cfg["hooks"])
        self.assertIn("StopFailure", cfg["hooks"])

    def test_print_codex_config_targets_permissionrequest(self):
        p = subprocess.run([sys.executable, APPROVE, "--print-codex-config"], input=b"",
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                           env=clean_env(), cwd=ROOT, timeout=60)
        cfg = json.loads(p.stdout.decode("utf-8"))
        cmd = cfg["hooks"]["PermissionRequest"][0]["hooks"][0]["command"]
        self.assertIn("--agent codex", cmd)


class TestWatchDone(unittest.TestCase):
    def test_stopfailure_rate_limit_extracts_reset(self):
        title, text, sound = wd.build_notification({
            "hook_event_name": "StopFailure",
            "error": "rate_limit",
            "last_assistant_message": "You've hit your session limit ∙ resets 1:10am (Asia/Shanghai)",
        })
        self.assertIn("额度已用完", title)
        self.assertIn("1:10am", text)
        self.assertEqual(sound, wd.LIMIT_SOUND)

    def test_stopfailure_other_error(self):
        title, text, sound = wd.build_notification({
            "hook_event_name": "StopFailure",
            "error": "api_error",
            "error_details": "boom",
        })
        self.assertIn("异常终止", title)
        self.assertIn("api_error", text)
        self.assertIn("boom", text)

    def test_normal_stop(self):
        title, text, sound = wd.build_notification({"hook_event_name": "Stop"})
        self.assertEqual(title, wd.DONE_TITLE)
        self.assertIsNone(sound)

    def _write_rollout(self, used_percent):
        line = json.dumps({
            "type": "event_msg",
            "payload": {"type": "token_count", "rate_limits": {
                "primary": {"used_percent": used_percent, "window_minutes": 300,
                            "resets_at": time.time() + 3600},
                "secondary": None,
            }},
        })
        f = tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False, encoding="utf-8")
        f.write("junk not json\n" + line + "\n")
        f.close()
        self.addCleanup(os.unlink, f.name)
        return f.name

    def test_read_codex_rate(self):
        path = self._write_rollout(95.0)
        pct, label, reset = wd.read_codex_rate(path)
        self.assertEqual(pct, 95.0)
        self.assertEqual(label, "5 小时")
        self.assertTrue(reset)

    def test_codex_limit_warning_replaces_done_notice(self):
        path = self._write_rollout(95.0)
        old_agent, old_name = wd.AGENT, wd.AGENT_NAME
        wd.AGENT, wd.AGENT_NAME = "codex", "Codex"
        try:
            title, text, sound = wd.build_notification(
                {"hook_event_name": "Stop", "transcript_path": path})
        finally:
            wd.AGENT, wd.AGENT_NAME = old_agent, old_name
        self.assertIn("95", title)
        self.assertEqual(sound, wd.LIMIT_SOUND)

    def test_codex_under_threshold_keeps_done_notice(self):
        path = self._write_rollout(10.0)
        old_agent = wd.AGENT
        wd.AGENT = "codex"
        try:
            title, _, sound = wd.build_notification(
                {"hook_event_name": "Stop", "transcript_path": path})
        finally:
            wd.AGENT = old_agent
        self.assertEqual(title, wd.DONE_TITLE)
        self.assertIsNone(sound)


if __name__ == "__main__":
    unittest.main()
