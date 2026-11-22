"""Unit and mock integration tests for ccw (standard library only)."""
from __future__ import annotations

import importlib.machinery
import importlib.util
import io
import json
import os
import signal
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from datetime import datetime, timedelta
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
CCW_PATH = os.path.join(HERE, "ccw")


def _load_ccw():
    loader = importlib.machinery.SourceFileLoader("ccw", CCW_PATH)
    spec = importlib.util.spec_from_loader("ccw", loader)
    module = importlib.util.module_from_spec(spec)
    sys.modules["ccw"] = module  # dataclasses need the module registered
    loader.exec_module(module)
    return module


ccw = _load_ccw()

NOW = datetime(2026, 11, 21, 10, 0, 0)


class FakeClock:
    def __init__(self, start: datetime) -> None:
        self.current = start
        self.slept = 0.0
        self.calls = 0

    def now(self) -> datetime:
        return self.current

    def sleep(self, seconds: float) -> None:
        self.calls += 1
        self.slept += seconds
        self.current += timedelta(seconds=seconds)


# --------------------------------------------------------------------------
# 1. Reset time parsing (12h / 24h / rollover / relative / epoch)
# --------------------------------------------------------------------------


class TestParseResetTime(unittest.TestCase):
    def parse(self, text, now=NOW):
        return ccw.parse_reset_time(text, now)

    def test_12h_pm_later_today(self):
        self.assertEqual(self.parse("limit resets at 3:00 PM"), datetime(2026, 11, 21, 15, 0))

    def test_12h_lowercase_am_already_passed_rolls_to_tomorrow(self):
        now = datetime(2026, 11, 21, 12, 0)
        self.assertEqual(self.parse("at 11:30 am", now), datetime(2026, 11, 22, 11, 30))

    def test_12h_without_minutes(self):
        self.assertEqual(self.parse("5-hour limit reached ∙ resets 3pm"), datetime(2026, 11, 21, 15, 0))

    def test_12h_dotted(self):
        self.assertEqual(self.parse("resets at 4 p.m."), datetime(2026, 11, 21, 16, 0))

    def test_12_am_is_midnight_next_day(self):
        self.assertEqual(self.parse("resets at 12 AM"), datetime(2026, 11, 22, 0, 0))

    def test_12_pm_is_noon(self):
        self.assertEqual(self.parse("resets at 12:00 PM"), datetime(2026, 11, 21, 12, 0))

    def test_24h_later_today(self):
        self.assertEqual(self.parse("resets at 15:00"), datetime(2026, 11, 21, 15, 0))

    def test_24h_midnight_rollover(self):
        now = datetime(2026, 11, 21, 23, 0)
        self.assertEqual(self.parse("resets at 03:30", now), datetime(2026, 11, 22, 3, 30))

    def test_rollover_across_month_end(self):
        now = datetime(2026, 11, 30, 22, 0)
        self.assertEqual(self.parse("resets at 01:00", now), datetime(2026, 12, 1, 1, 0))

    def test_same_minute_as_now_rolls_over(self):
        self.assertEqual(self.parse("at 10:00"), datetime(2026, 11, 22, 10, 0))

    def test_relative_hours_and_minutes(self):
        self.assertEqual(self.parse("try again in 2 hours 15 minutes"), NOW + timedelta(hours=2, minutes=15))

    def test_relative_minutes_only(self):
        self.assertEqual(self.parse("in 45 minutes"), NOW + timedelta(minutes=45))

    def test_relative_compact(self):
        self.assertEqual(self.parse("in 2h15m"), NOW + timedelta(hours=2, minutes=15))

    def test_epoch_format(self):
        self.assertEqual(
            self.parse("Claude AI usage limit reached|1700000000"),
            datetime.fromtimestamp(1700000000),
        )

    def test_invalid_times_are_rejected(self):
        self.assertIsNone(self.parse("resets at 25:00"))
        self.assertIsNone(self.parse("resets at 13 pm"))
        self.assertIsNone(self.parse("resets at 10:75"))

    def test_no_time(self):
        self.assertIsNone(self.parse("usage limit reached"))
        self.assertIsNone(self.parse("look in the docs"))

    def test_parse_limit_message(self):
        info = ccw.parse_limit_message("Session limit reached, resets at 3pm\n", NOW)
        self.assertTrue(info.detected)
        self.assertEqual(info.reset_at, datetime(2026, 11, 21, 15, 0))
        self.assertEqual(info.raw_line, "Session limit reached, resets at 3pm")

    def test_detection_variants(self):
        for line in (
            "5-hour limit reached",
            "You've hit your 5 hour limit",
            "USAGE LIMIT REACHED",
            "You've hit your limit · resets 3pm (Asia/Tokyo)",
        ):
            with self.subTest(line=line):
                self.assertTrue(ccw.parse_limit_message(line, NOW).detected)

    def test_time_on_following_line(self):
        info = ccw.detect_limit(["5-hour limit reached\n", "resets 3pm\n"], NOW)
        self.assertTrue(info.detected)
        self.assertEqual(info.reset_at, datetime(2026, 11, 21, 15, 0))


# --------------------------------------------------------------------------
# 2. Fallback wait calculation
# --------------------------------------------------------------------------


class TestComputeWait(unittest.TestCase):
    def test_unparseable_uses_fallback(self):
        info = ccw.detect_limit(["Usage limit reached. Please wait."], NOW)
        self.assertTrue(info.detected)
        self.assertIsNone(info.reset_at)
        self.assertEqual(ccw.compute_wait_seconds(info, NOW, 15, 18000), 18000.0)

    def test_parsed_time_adds_buffer(self):
        info = ccw.LimitInfo(True, NOW + timedelta(hours=1), "x")
        self.assertEqual(ccw.compute_wait_seconds(info, NOW, 15, 18000), 3615.0)

    def test_reset_in_past_waits_only_buffer(self):
        info = ccw.LimitInfo(True, NOW - timedelta(minutes=5), "x")
        self.assertEqual(ccw.compute_wait_seconds(info, NOW, 15, 18000), 15.0)

    def test_wait_until_reports_and_sleeps_to_target(self):
        clock = FakeClock(NOW)
        messages = []
        ccw.wait_until(NOW + timedelta(seconds=150), clock.now, clock.sleep, messages.append)
        self.assertEqual(clock.slept, 150.0)
        self.assertEqual(clock.calls, 3)  # 60 + 60 + 30
        self.assertEqual(len(messages), 3)


# --------------------------------------------------------------------------
# 3. Non-limit output must not trigger sleep
# --------------------------------------------------------------------------


class TestNonLimitPassThrough(unittest.TestCase):
    def test_plain_error_not_detected(self):
        lines = ["Error: API key invalid\n", "Traceback...\n"]
        self.assertFalse(ccw.detect_limit(lines, NOW).detected)

    def test_limit_phrase_far_from_tail_is_ignored(self):
        lines = ["The docs mention 'usage limit reached' messages.\n"] + [f"line {i}\n" for i in range(10)]
        self.assertFalse(ccw.detect_limit(lines, NOW).detected)

    def test_empty_output(self):
        self.assertFalse(ccw.detect_limit([], NOW).detected)


# --------------------------------------------------------------------------
# Secret redaction
# --------------------------------------------------------------------------


class TestRedaction(unittest.TestCase):
    def test_anthropic_key(self):
        out = ccw.redact_secrets("key=sk-ant-api03-AbCdEf0123456789_xyz")
        self.assertNotIn("AbCdEf0123456789", out)
        self.assertIn("sk-ant-***REDACTED***", out)

    def test_aws_secret_assignment(self):
        out = ccw.redact_secrets("AWS_SECRET_ACCESS_KEY=wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY")
        self.assertEqual(out, "AWS_SECRET_ACCESS_KEY=***REDACTED***")

    def test_aws_access_key_id(self):
        self.assertEqual(ccw.redact_secrets("AKIAIOSFODNN7EXAMPLE"), "AKIA***REDACTED***")

    def test_bearer_token(self):
        out = ccw.redact_secrets("Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.abc.def")
        self.assertEqual(out, "Authorization: Bearer ***REDACTED***")

    def test_github_token(self):
        out = ccw.redact_secrets("token ghp_0123456789abcdefghijABCDEFGHIJ")
        self.assertEqual(out, "token ghp_***REDACTED***")

    def test_env_token_with_quotes(self):
        out = ccw.redact_secrets('export GITHUB_TOKEN="abcd1234efgh"')
        self.assertEqual(out, 'export GITHUB_TOKEN="***REDACTED***"')

    def test_json_api_key(self):
        out = ccw.redact_secrets('{"api_key": "abcdef123456"}')
        self.assertEqual(out, '{"api_key": "***REDACTED***"}')

    def test_harmless_text_unchanged(self):
        for text in ("max_tokens: 1000", "Bearer of bad news", "sk-short", "hello world"):
            with self.subTest(text=text):
                self.assertEqual(ccw.redact_secrets(text), text)

    def test_multiline_private_key(self):
        r = ccw.Redactor()
        lines = [
            "before\n",
            "-----BEGIN RSA PRIVATE KEY-----\n",
            "MIIEowIBAAKCAQEAsecretbody\n",
            "-----END RSA PRIVATE KEY-----\n",
            "after\n",
        ]
        out = "".join(r.redact(line) for line in lines)
        self.assertNotIn("secretbody", out)
        self.assertIn("before\n", out)
        self.assertIn("after\n", out)
        self.assertIn("-----END RSA PRIVATE KEY-----", out)

    def test_inline_private_key(self):
        out = ccw.Redactor().redact("-----BEGIN PRIVATE KEY-----abc-----END PRIVATE KEY-----\n")
        self.assertNotIn("abc", out)
        self.assertTrue(out.endswith("\n"))


# --------------------------------------------------------------------------
# 4. Lock file
# --------------------------------------------------------------------------


class TestLock(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = self._tmp.name
        self.path = os.path.join(self.dir, ".ccw.lock")

    def tearDown(self):
        self._tmp.cleanup()

    def write_lock(self, content: str) -> None:
        with open(self.path, "w", encoding="utf-8") as fh:
            fh.write(content)

    def test_create_and_schema(self):
        path = ccw.acquire_lock(self.dir, pid=4242)
        self.assertEqual(path, self.path)
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        self.assertEqual(data["pid"], 4242)
        self.assertEqual(data["cwd"], os.path.abspath(self.dir))
        datetime.fromisoformat(data["started_at"])

    @unittest.skipIf(os.name == "nt", "POSIX permissions only")
    def test_posix_permissions_0600(self):
        old = os.umask(0)
        try:
            path = ccw.acquire_lock(self.dir)
        finally:
            os.umask(old)
        self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)

    def test_active_pid_rejected(self):
        self.write_lock(json.dumps({"pid": 999999, "started_at": "x", "cwd": self.dir}))
        with self.assertRaises(ccw.LockHeldError) as cm:
            ccw.acquire_lock(self.dir, pid=1, is_alive=lambda pid: True)
        self.assertEqual(cm.exception.pid, 999999)

    def test_own_pid_in_lock_is_treated_as_held(self):
        self.write_lock(json.dumps({"pid": 77, "started_at": "x", "cwd": self.dir}))
        with self.assertRaises(ccw.LockHeldError):
            ccw.acquire_lock(self.dir, pid=77, is_alive=lambda pid: False)

    def test_stale_pid_recovered(self):
        self.write_lock(json.dumps({"pid": 999999, "started_at": "x", "cwd": self.dir}))
        ccw.acquire_lock(self.dir, pid=5, is_alive=lambda pid: False)
        with open(self.path, encoding="utf-8") as fh:
            self.assertEqual(json.load(fh)["pid"], 5)

    def test_fresh_corrupt_lock_is_held(self):
        self.write_lock("{not json")
        with self.assertRaises(ccw.LockHeldError):
            ccw.acquire_lock(self.dir, pid=5, is_alive=lambda pid: False)

    def test_old_corrupt_lock_recovered(self):
        self.write_lock("{not json")
        old = time.time() - 3600
        os.utime(self.path, (old, old))
        ccw.acquire_lock(self.dir, pid=5, is_alive=lambda pid: False)
        with open(self.path, encoding="utf-8") as fh:
            self.assertEqual(json.load(fh)["pid"], 5)

    def test_symlink_rejected(self):
        target = os.path.join(self.dir, "victim.txt")
        with open(target, "w", encoding="utf-8") as fh:
            fh.write("keep me")
        try:
            os.symlink(target, self.path)
        except (OSError, NotImplementedError) as exc:
            self.skipTest(f"cannot create symlink here: {exc}")
        with self.assertRaises(ccw.LockError):
            ccw.acquire_lock(self.dir)
        with open(target, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "keep me")
        self.assertFalse(ccw.release_lock(self.path))
        self.assertTrue(os.path.islink(self.path))

    def test_release_only_own_lock(self):
        path = ccw.acquire_lock(self.dir, pid=10)
        self.assertFalse(ccw.release_lock(path, pid=11))
        self.assertTrue(os.path.exists(path))
        self.assertTrue(ccw.release_lock(path, pid=10))
        self.assertFalse(os.path.exists(path))
        self.assertFalse(ccw.release_lock(path, pid=10))
        self.assertFalse(ccw.release_lock(None))

    def test_is_pid_alive_self(self):
        self.assertTrue(ccw.is_pid_alive(os.getpid()))

    def test_is_pid_alive_dead_process(self):
        proc = subprocess.Popen([sys.executable, "-c", "pass"])
        proc.wait()
        self.assertFalse(ccw.is_pid_alive(proc.pid))

    def test_is_pid_alive_invalid(self):
        for pid in (0, -1, True, "12"):
            with self.subTest(pid=pid):
                self.assertFalse(ccw.is_pid_alive(pid))


# --------------------------------------------------------------------------
# claude command resolution
# --------------------------------------------------------------------------


class TestResolveClaude(unittest.TestCase):
    def test_env_override(self):
        env = {"CCW_CLAUDE_CMD": json.dumps(["/usr/bin/python3", "mock.py"])}
        self.assertEqual(ccw.resolve_claude_command(env, which=lambda n: None, is_windows=False),
                         ["/usr/bin/python3", "mock.py"])

    def test_env_override_invalid(self):
        for raw in ("not json", "[]", '"str"', "[1]"):
            with self.subTest(raw=raw), self.assertRaises(ccw.ClaudeNotFoundError):
                ccw.resolve_claude_command({"CCW_CLAUDE_CMD": raw}, which=lambda n: None)

    def test_env_override_batch_rejected_on_windows(self):
        env = {"CCW_CLAUDE_CMD": json.dumps(["C:\\npm\\claude.cmd"])}
        with self.assertRaises(ccw.ClaudeNotFoundError):
            ccw.resolve_claude_command(env, which=lambda n: None, is_windows=True)

    def test_not_found(self):
        with self.assertRaises(ccw.ClaudeNotFoundError):
            ccw.resolve_claude_command({}, which=lambda n: None, is_windows=False)

    def test_posix_path(self):
        self.assertEqual(
            ccw.resolve_claude_command({}, which=lambda n: "/usr/local/bin/claude", is_windows=False),
            ["/usr/local/bin/claude"],
        )

    def test_windows_exe_used_directly(self):
        self.assertEqual(
            ccw.resolve_claude_command({}, which=lambda n: "C:\\x\\claude.exe", is_windows=True),
            ["C:\\x\\claude.exe"],
        )

    def _make_npm_dir(self, tmp, files):
        pkg = os.path.join(tmp, "node_modules", "@anthropic-ai", "claude-code")
        for rel in files:
            full = os.path.join(pkg, rel)
            os.makedirs(os.path.dirname(full), exist_ok=True)
            open(full, "w").close()
        shim = os.path.join(tmp, "claude.cmd")
        open(shim, "w").close()
        return pkg, shim

    def test_windows_cmd_shim_resolves_native_exe(self):
        with tempfile.TemporaryDirectory() as tmp:
            pkg, shim = self._make_npm_dir(tmp, [os.path.join("bin", "claude.exe")])
            cmd = ccw.resolve_claude_command({}, which=lambda n: shim, is_windows=True)
            self.assertEqual(cmd, [os.path.join(pkg, "bin", "claude.exe")])

    def test_windows_cmd_shim_resolves_node_cli(self):
        with tempfile.TemporaryDirectory() as tmp:
            pkg, shim = self._make_npm_dir(tmp, ["cli.js"])
            which = {"claude": shim, "node": "C:\\node\\node.exe"}.get
            cmd = ccw.resolve_claude_command({}, which=which, is_windows=True)
            self.assertEqual(cmd, ["C:\\node\\node.exe", os.path.join(pkg, "cli.js")])

    def test_windows_cmd_shim_without_target_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, shim = self._make_npm_dir(tmp, [])
            with self.assertRaises(ccw.ClaudeNotFoundError):
                ccw.resolve_claude_command({}, which=lambda n: shim, is_windows=True)


# --------------------------------------------------------------------------
# CLI parsing
# --------------------------------------------------------------------------


class TestOptions(unittest.TestCase):
    def parse(self, argv, stdin_text=""):
        return ccw.parse_options(argv, io.StringIO(stdin_text))

    def test_positional_prompt(self):
        opts = self.parse(["hello"])
        self.assertEqual(opts.initial_args(), ["-p", "hello"])
        self.assertIsNone(opts.initial_stdin)
        self.assertEqual(opts.resume_prompt, "作業を再開してください")
        self.assertEqual((opts.max_retries, opts.fallback_wait, opts.buffer_sec), (5, 18000, 15))

    def test_claude_options_are_carried_to_resume(self):
        opts = self.parse(["--max-retries", "2", "--resume-prompt", "go", "x", "--",
                           "--model", "opus", "--allowedTools", "Bash(git:*)", "Edit"])
        self.assertEqual(opts.max_retries, 2)
        extra = ["--model", "opus", "--allowedTools", "Bash(git:*)", "Edit"]
        self.assertEqual(opts.claude_args, extra)
        # "-p" comes after the options so a variadic option cannot swallow the prompt
        self.assertEqual(opts.initial_args(), extra + ["-p", "x"])
        self.assertEqual(opts.resume_args(), extra + ["--continue", "-p", "go"])

    def test_reserved_claude_options_rejected(self):
        for opt in ("-p", "--print", "-c", "--continue", "-r", "--resume", "--resume=abc"):
            with self.subTest(opt=opt), self.assertRaises(ccw.UsageError):
                self.parse(["x", "--", "--model", "opus", opt])

    def test_stdin_keeps_prompt_out_of_args(self):
        opts = self.parse(["--stdin", "--", "--model", "opus"], "secret prompt")
        self.assertEqual(opts.initial_args(), ["--model", "opus", "-p"])
        self.assertEqual(opts.initial_stdin, "secret prompt")

    def test_prompt_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "p.txt")
            with open(path, "w", encoding="utf-8-sig") as fh:
                fh.write("ファイルのプロンプト")
            opts = self.parse(["--prompt-file", path])
        self.assertEqual(opts.initial_args(), ["-p"])
        self.assertEqual(opts.initial_stdin, "ファイルのプロンプト")

    def test_errors(self):
        cases = [
            [],
            ["a", "--stdin"],
            ["a", "--", "-p", "b"],
            ["--stdin", "--prompt-file", "x"],
            ["--"],
            ["--", "--model", "opus"],  # options but no prompt
            ["a", "--"],                # empty option list
            ["   "],
            ["--max-retries", "-1", "a"],
            ["--buffer-sec", "abc", "a"],
            ["--prompt-file", os.path.join(HERE, "does-not-exist.txt")],
            ["--resume-prompt", "", "a"],
            ["--unknown", "a"],
        ]
        for argv in cases:
            with self.subTest(argv=argv), self.assertRaises(ccw.UsageError):
                self.parse(argv)

    def test_empty_stdin(self):
        with self.assertRaises(ccw.UsageError):
            self.parse(["--stdin"], "  \n")


# --------------------------------------------------------------------------
# 5. End-to-end loop with a mock claude executable
# --------------------------------------------------------------------------

MOCK_CLAUDE = textwrap.dedent(
    r'''
    import json, os, sys
    state_path = os.environ["MOCK_STATE"]
    try:
        with open(state_path, encoding="utf-8") as fh:
            state = json.load(fh)
    except FileNotFoundError:
        state = {"calls": []}
    stdin_text = None if sys.stdin is None or sys.stdin.isatty() else sys.stdin.read()
    state["calls"].append({"argv": sys.argv[1:], "stdin": stdin_text, "pid": os.getpid(),
                           "pythonutf8": os.environ.get("PYTHONUTF8")})
    with open(state_path, "w", encoding="utf-8") as fh:
        json.dump(state, fh)
    n = len(state["calls"])
    mode = os.environ.get("MOCK_MODE", "limit_then_ok")
    print("working... token sk-ant-api03-SECRETSECRETSECRET", flush=True)
    if mode == "hang":
        import time
        time.sleep(60)
        sys.exit(0)
    if mode == "error":
        print("Error: something broke", file=sys.stderr, flush=True)
        sys.exit(3)
    if mode == "interrupted":
        print("Claude usage limit reached. Your limit will reset at 3pm.", flush=True)
        sys.exit(130)
    if mode == "always_limit" or (mode == "limit_then_ok" and n == 1):
        default = "Claude usage limit reached. Your limit will reset at 3pm."
        print(os.environ.get("MOCK_LIMIT_LINE", default), flush=True)
        sys.exit(1)
    print("日本語の出力: done", flush=True)
    sys.exit(0)
    '''
)


class MockClaudeTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = self._tmp.name
        self.work = os.path.join(self.dir, "work")
        os.mkdir(self.work)
        self.mock = os.path.join(self.dir, "mock_claude.py")
        with open(self.mock, "w", encoding="utf-8") as fh:
            fh.write(MOCK_CLAUDE)
        self.state = os.path.join(self.dir, "state.json")
        self.claude_cmd = [sys.executable, self.mock]
        self.env = mock.patch.dict(os.environ, {"MOCK_STATE": self.state})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self._tmp.cleanup()

    def calls(self):
        try:
            with open(self.state, encoding="utf-8") as fh:
                return json.load(fh)["calls"]
        except FileNotFoundError:
            return []

    def run_main(self, argv, mode="limit_then_ok", stdin_text="", clock=None, **env):
        os.environ["MOCK_MODE"] = mode
        os.environ.update(env)
        clock = clock or FakeClock(NOW)
        out, err = io.StringIO(), io.StringIO()
        rc = ccw.main(
            argv,
            claude_cmd=self.claude_cmd,
            cwd=self.work,
            stdin=io.StringIO(stdin_text),
            stdout=out,
            stderr=err,
            now_fn=clock.now,
            sleep_fn=clock.sleep,
        )
        return rc, out.getvalue(), err.getvalue(), clock


class TestEndToEndInProcess(MockClaudeTestCase):
    def test_limit_then_resume_success(self):
        rc, out, err, clock = self.run_main(["--resume-prompt", "続けて", "初回プロンプト"])
        self.assertEqual(rc, 0, err)
        calls = self.calls()
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0]["argv"], ["-p", "初回プロンプト"])
        self.assertEqual(calls[1]["argv"], ["--continue", "-p", "続けて"])
        self.assertEqual(calls[0]["pythonutf8"], "1")
        # 10:00 -> 15:00 plus the 15 second buffer
        self.assertAlmostEqual(clock.slept, 5 * 3600 + 15, places=3)
        self.assertIn("日本語の出力: done", out)
        self.assertNotIn("SECRETSECRET", out)
        self.assertIn("sk-ant-***REDACTED***", out)
        self.assertFalse(os.path.exists(os.path.join(self.work, ".ccw.lock")))

    def test_claude_options_survive_resume(self):
        rc, _, err, _ = self.run_main(
            ["初回", "--", "--model", "opus", "--permission-mode", "acceptEdits"]
        )
        self.assertEqual(rc, 0, err)
        calls = self.calls()
        extra = ["--model", "opus", "--permission-mode", "acceptEdits"]
        self.assertEqual(calls[0]["argv"], extra + ["-p", "初回"])
        self.assertEqual(calls[1]["argv"], extra + ["--continue", "-p", "作業を再開してください"])

    def test_stdin_prompt_is_piped_not_in_argv(self):
        rc, _, err, _ = self.run_main(["--stdin"], mode="ok", stdin_text="秘密の指示")
        self.assertEqual(rc, 0, err)
        call = self.calls()[0]
        self.assertEqual(call["argv"], ["-p"])
        self.assertEqual(call["stdin"].replace("\r\n", "\n"), "秘密の指示")

    def test_non_limit_error_propagates_without_sleep(self):
        rc, out, _, clock = self.run_main(["x"], mode="error")
        self.assertEqual(rc, 3)
        self.assertEqual(clock.calls, 0)
        self.assertIn("Error: something broke", out)  # stderr merged into stdout
        self.assertEqual(len(self.calls()), 1)

    def test_interrupted_claude_is_not_resumed(self):
        # Even with a limit line in its output, an interrupted claude (130) ends ccw.
        rc, _, err, clock = self.run_main(["x"], mode="interrupted")
        self.assertEqual(rc, 130, err)
        self.assertEqual(clock.calls, 0)
        self.assertEqual(len(self.calls()), 1)
        self.assertIn("interrupted", err)
        self.assertFalse(os.path.exists(os.path.join(self.work, ".ccw.lock")))

    def test_max_retries_exceeded(self):
        rc, _, err, _ = self.run_main(["--max-retries", "1", "x"], mode="always_limit")
        self.assertEqual(rc, 2, err)
        self.assertEqual(len(self.calls()), 2)
        self.assertFalse(os.path.exists(os.path.join(self.work, ".ccw.lock")))

    def test_zero_retries(self):
        rc, _, _, clock = self.run_main(["--max-retries", "0", "x"], mode="always_limit")
        self.assertEqual(rc, 2)
        self.assertEqual(clock.calls, 0)

    def test_fallback_wait_when_unparseable(self):
        rc, _, err, clock = self.run_main(
            ["--fallback-wait", "120", "x"], MOCK_LIMIT_LINE="usage limit reached, sorry"
        )
        self.assertEqual(rc, 0, err)
        self.assertAlmostEqual(clock.slept, 120.0)
        self.assertIn("fallback", err)

    def test_interrupt_during_wait_cleans_up(self):
        class InterruptingClock(FakeClock):
            def sleep(self, seconds):
                raise KeyboardInterrupt

        rc, _, _, _ = self.run_main(["x"], clock=InterruptingClock(NOW))
        self.assertEqual(rc, 130)
        self.assertFalse(os.path.exists(os.path.join(self.work, ".ccw.lock")))

    def test_lock_held_exits_1_without_running(self):
        # A lock owned by a live process (this test process itself).
        held = ccw.acquire_lock(self.work)
        rc, _, err, _ = self.run_main(["x"])
        self.assertEqual(rc, 1)
        self.assertIn("already running", err)
        self.assertEqual(self.calls(), [])
        self.assertTrue(os.path.exists(held))

    def test_dry_run_does_not_invoke_claude(self):
        rc, _, err, _ = self.run_main(["--dry-run", "x"])
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.calls(), [])
        self.assertIn("lock acquired", err)
        self.assertNotIn("'x'", err)  # prompt text is not echoed
        self.assertFalse(os.path.exists(os.path.join(self.work, ".ccw.lock")))

    def test_usage_error_exit_1(self):
        rc, _, err, _ = self.run_main([])
        self.assertEqual(rc, 1)
        self.assertIn("error", err)


class TestEndToEndSubprocess(MockClaudeTestCase):
    """Runs the real ccw script as a child process."""

    def run_ccw(self, args, mode="limit_then_ok", input_text=None, **extra_env):
        env = dict(os.environ)
        env.update({
            "CCW_CLAUDE_CMD": json.dumps(self.claude_cmd),
            "MOCK_MODE": mode,
            "PYTHONIOENCODING": "utf-8",
        })
        env.update(extra_env)
        return subprocess.run(
            [sys.executable, CCW_PATH] + args,
            cwd=self.work,
            env=env,
            input=input_text,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
            shell=False,
        )

    def test_limit_then_continue_success(self):
        result = self.run_ccw(
            ["--fallback-wait", "0", "--buffer-sec", "0", "hello"],
            MOCK_LIMIT_LINE="5-hour limit reached",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = self.calls()
        self.assertEqual([c["argv"] for c in calls],
                         [["-p", "hello"], ["--continue", "-p", "作業を再開してください"]])
        self.assertIn("日本語の出力: done", result.stdout)
        self.assertNotIn("SECRETSECRET", result.stdout)
        self.assertFalse(os.path.exists(os.path.join(self.work, ".ccw.lock")))

    def test_prompt_file_via_subprocess(self):
        prompt = os.path.join(self.dir, "prompt.txt")
        with open(prompt, "w", encoding="utf-8") as fh:
            fh.write("file prompt")
        result = self.run_ccw(["--prompt-file", prompt], mode="ok")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.calls()[0]["argv"], ["-p"])
        self.assertEqual(self.calls()[0]["stdin"].strip(), "file prompt")

    def test_shell_metacharacters_are_literal(self):
        payload = 'a & echo pwned > pwned.txt | "b" $(whoami) `id`'
        result = self.run_ccw([payload], mode="ok")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.calls()[0]["argv"], ["-p", payload])
        self.assertFalse(os.path.exists(os.path.join(self.work, "pwned.txt")))

    def test_piped_output_is_utf8_without_pythonioencoding(self):
        env = dict(os.environ)
        env.pop("PYTHONIOENCODING", None)
        env.pop("PYTHONUTF8", None)
        env.update({"CCW_CLAUDE_CMD": json.dumps(self.claude_cmd), "MOCK_MODE": "ok"})
        result = subprocess.run(
            [sys.executable, CCW_PATH, "x"], cwd=self.work, env=env,
            capture_output=True, timeout=60, shell=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("日本語の出力: done", result.stdout.decode("utf-8"))

    def _start_hanging_ccw(self, **popen_kwargs):
        env = dict(os.environ)
        env.update({"CCW_CLAUDE_CMD": json.dumps(self.claude_cmd), "MOCK_MODE": "hang"})
        errfile = tempfile.TemporaryFile()  # noqa: SIM115 -- kept open past this method; closed by addCleanup
        self.addCleanup(errfile.close)
        proc = subprocess.Popen(
            [sys.executable, CCW_PATH, "x"], cwd=self.work, env=env,
            stdout=subprocess.DEVNULL, stderr=errfile, shell=False, **popen_kwargs,
        )
        proc.errfile = errfile
        self.addCleanup(lambda: proc.poll() is None and proc.kill())
        lock = os.path.join(self.work, ".ccw.lock")
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if os.path.exists(lock) and self.calls():
                return proc, lock, self.calls()[0]["pid"]
            time.sleep(0.1)
        self.fail("ccw did not start the mock claude in time")

    def _assert_cleaned_up(self, proc, lock, child_pid):
        rc = proc.wait(timeout=20)
        proc.errfile.seek(0)
        stderr = proc.errfile.read().decode("utf-8", "replace")
        self.assertEqual(rc, 130, "ccw stderr:\n" + stderr)
        self.assertFalse(os.path.exists(lock))
        deadline = time.monotonic() + 10
        while ccw.is_pid_alive(child_pid) and time.monotonic() < deadline:
            time.sleep(0.1)
        self.assertFalse(ccw.is_pid_alive(child_pid), "mock claude is still running")

    @unittest.skipIf(os.name == "nt", "POSIX signals")
    def test_sighup_and_sigterm_clean_up(self):
        for name in ("SIGHUP", "SIGTERM"):
            with self.subTest(signal=name):
                if os.path.exists(self.state):
                    os.remove(self.state)
                proc, lock, child_pid = self._start_hanging_ccw()
                proc.send_signal(getattr(signal, name))
                self._assert_cleaned_up(proc, lock, child_pid)

    @unittest.skipUnless(os.name == "nt", "Windows Ctrl+Break")
    def test_ctrl_break_cleans_up(self):
        proc, lock, child_pid = self._start_hanging_ccw(creationflags=subprocess.CREATE_NEW_PROCESS_GROUP)
        try:
            os.kill(proc.pid, signal.CTRL_BREAK_EVENT)
        except OSError as exc:
            self.skipTest(f"cannot send Ctrl+Break here (no console?): {exc}")
        self._assert_cleaned_up(proc, lock, child_pid)

    def test_invalid_args_exit_1(self):
        result = self.run_ccw(["--max-retries", "nope", "x"])
        self.assertEqual(result.returncode, 1)


class TestCleanupHandlers(unittest.TestCase):
    class FakeCleanup:
        def __init__(self):
            self.calls = []

        def run(self, wait_for_child=True):
            self.calls.append(wait_for_child)

    def test_console_close_cleans_up_without_waiting(self):
        for event in (ccw.CTRL_CLOSE_EVENT, ccw.CTRL_LOGOFF_EVENT, ccw.CTRL_SHUTDOWN_EVENT):
            with self.subTest(event=event):
                cleanup = self.FakeCleanup()
                self.assertTrue(ccw._console_ctrl_handler(event, cleanup))
                self.assertEqual(cleanup.calls, [False])

    def test_ctrl_c_and_break_left_to_python(self):
        for event in (0, 1):  # CTRL_C_EVENT, CTRL_BREAK_EVENT
            with self.subTest(event=event):
                cleanup = self.FakeCleanup()
                self.assertFalse(ccw._console_ctrl_handler(event, cleanup))
                self.assertEqual(cleanup.calls, [])

    def test_cleanup_runs_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            runner = mock.Mock()
            cleanup = ccw._Cleanup(runner)
            cleanup.lock_path = ccw.acquire_lock(tmp)
            cleanup.run(wait_for_child=False)
            cleanup.run()
            runner.terminate.assert_called_once_with(wait=False)
            self.assertFalse(os.path.exists(cleanup.lock_path))

    def test_handlers_installed_and_restored(self):
        before = signal.getsignal(signal.SIGTERM)
        restore = ccw._install_handlers(ccw._Cleanup(mock.Mock()))
        try:
            self.assertIs(signal.getsignal(signal.SIGTERM), ccw._raise_terminated)
            if hasattr(signal, "SIGHUP"):
                self.assertIs(signal.getsignal(signal.SIGHUP), ccw._raise_terminated)
        finally:
            restore()
        self.assertEqual(signal.getsignal(signal.SIGTERM), before)


class TestExitCodeNormalization(unittest.TestCase):
    def test_posix_signal(self):
        self.assertEqual(ccw._normalize_exit_code(-2), 130)
        self.assertEqual(ccw._normalize_exit_code(-15), 143)

    def test_windows_ctrl_c_exit(self):
        self.assertEqual(ccw._normalize_exit_code(0xC000013A), 130)

    def test_ordinary_codes_unchanged(self):
        for rc in (0, 1, 2, 3221225477):
            self.assertEqual(ccw._normalize_exit_code(rc), rc)


class TestOutputEncoding(unittest.TestCase):
    class Stream:
        def __init__(self, tty):
            self.tty = tty

        def isatty(self):
            return self.tty

    def test_windows_pipe_forces_utf8(self):
        self.assertTrue(ccw._wants_utf8(self.Stream(False), {}, is_windows=True))

    def test_windows_console_left_alone(self):
        self.assertFalse(ccw._wants_utf8(self.Stream(True), {}, is_windows=True))

    def test_pythonioencoding_wins(self):
        env = {"PYTHONIOENCODING": "cp932"}
        self.assertFalse(ccw._wants_utf8(self.Stream(False), env, is_windows=True))

    def test_posix_left_alone(self):
        self.assertFalse(ccw._wants_utf8(self.Stream(False), {}, is_windows=False))

    def test_stream_without_isatty(self):
        self.assertFalse(ccw._wants_utf8(object(), {}, is_windows=True))


# --------------------------------------------------------------------------
# 6. Interactive mode (-i)
# --------------------------------------------------------------------------

# A frame as a TUI draws it: erase/move the cursor, colors, a title (OSC) and
# the limit message split by style changes.
TUI_LIMIT_FRAME = (
    "\x1b]0;✳ claude\x07"
    "\x1b[2K\x1b[1A\x1b[2K\x1b[G"
    "\x1b[38;2;215;119;87m⎿\x1b[39m  \x1b[1mClaude usage limit reached.\x1b[22m"
    " Your limit will reset at \x1b[1m3pm\x1b[22m (Asia/Tokyo).\r\n"
    "\x1b[2K\x1b[G╭──────────────╮\r\n"
    "\x1b[2K\x1b[G│ > \x1b[7m \x1b[27m          │\r\n"
    "\x1b[2K\x1b[G╰──────────────╯\x1b[?25h"
)


class TestAnsiStrip(unittest.TestCase):
    def test_strip_removes_sequences_and_keeps_text(self):
        text = ccw.strip_ansi("\x1b[31mred\x1b[0m plain \x1b]8;;https://x\x1b\\link\x1b]8;;\x1b\\\x1b(B\x07")
        self.assertEqual(text, "red plain link")

    def test_cursor_moves_separate_rows(self):
        self.assertEqual(ccw.strip_ansi("one\x1b[1Btwo\x1b[3;1Hthree\x1b[2Cfour"), "one\ntwo\nthree four")

    def test_limit_message_is_parsed_after_stripping(self):
        lines = ccw.strip_ansi(TUI_LIMIT_FRAME).splitlines()
        # The input box is drawn below the message, so the whole frame is inspected.
        info = ccw.detect_limit(lines, NOW, tail_lines=None)
        self.assertTrue(info.detected)
        self.assertEqual(info.reset_at, datetime(2026, 11, 21, 15, 0))
        self.assertNotIn("\x1b", info.raw_line)
        self.assertIn("Claude usage limit reached. Your limit will reset at 3pm", info.raw_line)

    def test_raw_text_does_not_parse_the_time(self):
        # Without stripping, "\x1b[1m" sits between "at" and "3pm".
        self.assertIsNone(ccw.parse_reset_time("reset at \x1b[1m3pm\x1b[22m", NOW))
        self.assertEqual(ccw.parse_reset_time(ccw.strip_ansi("reset at \x1b[1m3pm\x1b[22m"), NOW),
                         datetime(2026, 11, 21, 15, 0))


class TestTerminalRedactor(unittest.TestCase):
    def run_chunks(self, chunks):
        r = ccw.TerminalRedactor()
        return "".join(r.feed(c) for c in chunks) + r.flush()

    def test_escape_sequences_untouched_and_width_kept(self):
        raw = "\x1b[31mkey sk-ant-api03-AbCdEf0123456789\x1b[0m\x1b[5;10H"
        out = self.run_chunks([raw])
        self.assertNotIn("AbCdEf0123456789", out)
        self.assertEqual(len(out), len(raw))
        self.assertTrue(out.startswith("\x1b[31mkey sk-ant-****"))
        self.assertTrue(out.endswith("\x1b[0m\x1b[5;10H"))

    def test_secret_split_across_reads(self):
        out = self.run_chunks(["token sk-ant-api03-Ab", "CdEf0123456789 done\x1b[0m"])
        self.assertNotIn("CdEf0123456789", out)
        self.assertIn("done\x1b[0m", out)

    def test_escape_split_across_reads(self):
        chunks = ["a\x1b[3", "1mb\x1b]0;ti", "tle\x07c"]
        self.assertEqual(self.run_chunks(chunks), "".join(chunks))

    def test_same_width_rules(self):
        for text in ('AWS_SECRET_ACCESS_KEY=wJalrXUtnFEMI/K7MDENG', '{"api_key": "abcdef123456"}',
                     "Bearer eyJhbGciOiJIUzI1NiJ9.abc", "-----BEGIN PRIVATE KEY-----abc-----END PRIVATE KEY-----"):
            with self.subTest(text=text):
                out = ccw.redact_secrets_same_width(text)
                self.assertEqual(len(out), len(text))
                self.assertNotEqual(out, text)
        self.assertEqual(ccw.redact_secrets_same_width("max_tokens: 1000"), "max_tokens: 1000")


class TestAutoResumer(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock(NOW)
        self.mono = 100.0
        self.messages = []
        self.resumer = self.make()

    def make(self, max_retries=5):
        return ccw.AutoResumer("続けて", max_retries, 15, 18000, self.clock.now, self.messages.append)

    def advance(self, seconds):
        self.mono += seconds
        self.clock.current += timedelta(seconds=seconds)

    def poll(self):
        return self.resumer.poll(self.mono)

    def hit_limit(self, frame=TUI_LIMIT_FRAME):
        self.resumer.on_output(frame, self.mono)
        self.advance(ccw.OUTPUT_SETTLE_SEC + 0.01)
        self.assertEqual(self.poll(), b"")

    def test_resume_prompt_sent_once_then_reset(self):
        self.hit_limit()
        self.assertEqual(self.resumer.state, "waiting")
        self.assertEqual(self.resumer.target, datetime(2026, 11, 21, 15, 0, 15))
        # Output during the wait (the TUI redrawing the same screen) changes nothing.
        self.resumer.on_output(TUI_LIMIT_FRAME, self.mono)
        self.advance(5 * 3600 + 14)
        self.assertEqual(self.poll(), b"")  # 1 second before the target
        self.advance(1)
        self.assertEqual(self.poll(), "続けて".encode())
        self.assertEqual(self.resumer.state, "submitting")
        self.assertEqual(self.poll(), b"")  # "\r" only after the gap
        self.advance(ccw.RESUME_SUBMIT_DELAY_SEC)
        self.assertEqual(self.poll(), b"\r")
        self.assertEqual(self.resumer.state, "watching")
        self.assertEqual(self.resumer.retry_count, 1)
        # A redraw of the transcript still shows the handled line: no second wait.
        self.hit_limit()
        self.assertEqual(self.resumer.state, "watching")
        self.advance(10 * 3600)
        self.assertEqual(self.poll(), b"")

    def test_waits_for_user_to_stop_typing(self):
        self.hit_limit()
        self.advance(5 * 3600 + 15)
        self.resumer.on_input(self.mono)
        self.assertEqual(self.poll(), b"")
        self.assertAlmostEqual(self.resumer.timeout(self.mono), ccw.INPUT_IDLE_SEC)
        self.advance(ccw.INPUT_IDLE_SEC)
        self.assertEqual(self.poll(), "続けて".encode())

    def test_waits_for_output_to_settle(self):
        self.resumer.on_output(TUI_LIMIT_FRAME, self.mono)
        self.assertEqual(self.poll(), b"")
        self.assertEqual(self.resumer.state, "watching")
        self.assertAlmostEqual(self.resumer.timeout(self.mono), ccw.OUTPUT_SETTLE_SEC)

    def test_new_limit_after_resume_is_detected(self):
        self.hit_limit()
        self.advance(5 * 3600 + 15)
        self.assertEqual(self.poll(), "続けて".encode())
        self.advance(1)
        self.assertEqual(self.poll(), b"\r")
        self.hit_limit("\x1b[1m5-hour limit reached ∙ resets 11pm\x1b[0m\r\n")
        self.assertEqual(self.resumer.state, "waiting")

    def test_fallback_wait(self):
        self.hit_limit("\x1b[31musage limit reached\x1b[0m\r\n")
        self.assertEqual(self.resumer.target, self.clock.now() + timedelta(seconds=18000))
        self.assertTrue(any("fallback" in m for m in self.messages))

    def test_max_retries_gives_up(self):
        self.resumer = self.make(max_retries=0)
        self.hit_limit()
        self.assertTrue(self.resumer.gave_up)
        self.advance(24 * 3600)
        self.assertEqual(self.poll(), b"")
        self.assertIsNone(self.resumer.timeout(self.mono))

    def test_plain_output_is_ignored(self):
        self.hit_limit("\x1b[32mAll tests passed\x1b[0m\r\n> ")
        self.assertEqual(self.resumer.state, "watching")


class TestInteractiveOptions(unittest.TestCase):
    def parse(self, argv):
        return ccw.parse_options(argv, io.StringIO(""))

    def test_prompt_is_optional(self):
        opts = self.parse(["-i"])
        self.assertTrue(opts.interactive)
        self.assertEqual(opts.interactive_args(), [])

    def test_prompt_and_options(self):
        opts = self.parse(["--interactive", "初回", "--", "--allowedTools", "Edit", "--continue"])
        self.assertEqual(opts.interactive_args(), ["--allowedTools", "Edit", "--continue", "--", "初回"])

    def test_print_rejected_and_stdin_sources_rejected(self):
        for argv in (["-i", "--", "-p"], ["-i", "--", "--print"], ["-i", "--stdin"],
                     ["-i", "--prompt-file", "x"], ["-i", "  "], ["-i", "--"]):
            with self.subTest(argv=argv), self.assertRaises(ccw.UsageError):
                self.parse(argv)

    def test_headless_still_requires_prompt(self):
        with self.assertRaises(ccw.UsageError):
            self.parse([])

    def test_dry_run(self):
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(ccw, "interactive_unsupported_reason", return_value=None):
            err = io.StringIO()
            rc = ccw.main(["-i", "--dry-run", "secret", "--", "--model", "opus"], claude_cmd=["claude"],
                          cwd=tmp, stdin=io.StringIO(), stdout=io.StringIO(), stderr=err)
            self.assertFalse(os.path.exists(os.path.join(tmp, ".ccw.lock")))
        self.assertEqual(rc, 0, err.getvalue())
        self.assertIn("['claude', '--model', 'opus', '--', '<prompt>']", err.getvalue())
        self.assertNotIn("secret", err.getvalue())


class TestInteractiveWindows(unittest.TestCase):
    EXPECTED = "Interactive mode (-i) requires a POSIX environment (WSL2 or macOS)"

    def test_native_windows_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            out, err = io.StringIO(), io.StringIO()
            with mock.patch.object(ccw.os, "name", "nt"):
                rc = ccw.main(["-i", "--", "--model", "opus"], claude_cmd=["claude"], cwd=tmp,
                              stdin=io.StringIO(), stdout=out, stderr=err)
            self.assertFalse(os.path.exists(os.path.join(tmp, ".ccw.lock")))
        self.assertEqual(rc, 1)
        self.assertIn(self.EXPECTED, err.getvalue())
        self.assertIn("wsl ccw -i", err.getvalue())
        self.assertEqual(out.getvalue(), "")

    @unittest.skipUnless(os.name == "nt", "native Windows only")
    def test_native_windows_subprocess_has_no_traceback(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = subprocess.run(
                [sys.executable, CCW_PATH, "--interactive"], cwd=tmp, capture_output=True,
                text=True, encoding="utf-8", errors="replace", timeout=60, shell=False,
            )
            self.assertFalse(os.path.exists(os.path.join(tmp, ".ccw.lock")))
        self.assertEqual(result.returncode, 1)
        self.assertIn(self.EXPECTED, result.stderr)
        self.assertNotIn("Traceback", result.stderr)


# A dummy TUI: raw mode, draws a limit message with ANSI codes, then records
# what arrives on its stdin (expected: resume prompt + "\r", exactly once).
DUMMY_TUI = textwrap.dedent(
    r'''
    import fcntl, json, os, select, struct, sys, termios, tty
    def winsize():
        rows, cols, _, _ = struct.unpack("HHHH", fcntl.ioctl(0, termios.TIOCGWINSZ, b"\0" * 8))
        return [rows, cols]
    fd = sys.stdin.fileno()
    first_size = winsize()
    saved = termios.tcgetattr(fd)
    tty.setraw(fd)
    received = b""
    extra = b""
    try:
        os.write(1, b"\x1b[2J\x1b[H\x1b[1mWelcome\x1b[0m\r\n")
        os.write(1, "\x1b[31m5-hour limit reached\x1b[0m ∙ resets in 0 minutes\r\n".encode())
        os.write(1, b"\x1b[2K> \x1b[7m \x1b[27m")
        while not received.endswith(b"\r"):
            ready, _, _ = select.select([fd], [], [], 30)
            if not ready:
                break
            received += os.read(fd, 1024)
        while True:  # anything sent after the first "\r" would be a double send
            ready, _, _ = select.select([fd], [], [], 1.5)
            if not ready:
                break
            chunk = os.read(fd, 1024)
            if not chunk:
                break
            extra += chunk
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, saved)
    with open(os.environ["MOCK_STATE"], "w", encoding="utf-8") as fh:
        json.dump({"argv": sys.argv[1:], "received": received.decode("utf-8", "replace"),
                   "extra": extra.decode("utf-8", "replace"), "isatty": os.isatty(fd),
                   "first_size": first_size, "last_size": winsize()}, fh)
    os.write(1, b"resumed ok\r\n")
    '''
)


@unittest.skipIf(os.name == "nt", "PTY requires POSIX")
class TestInteractivePty(unittest.TestCase):
    def setUp(self):
        try:
            import pty  # noqa: F401
        except ImportError:
            self.skipTest("pty is not available")
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.work = os.path.join(self._tmp.name, "work")
        os.mkdir(self.work)
        self.state = os.path.join(self._tmp.name, "state.json")
        dummy = os.path.join(self._tmp.name, "dummy_tui.py")
        with open(dummy, "w", encoding="utf-8") as fh:
            fh.write(DUMMY_TUI)
        self.env = dict(os.environ)
        self.env.update({"CCW_CLAUDE_CMD": json.dumps([sys.executable, dummy]), "MOCK_STATE": self.state})

    def result(self):
        with open(self.state, encoding="utf-8") as fh:
            return json.load(fh)

    def test_resume_prompt_typed_into_same_session(self):
        args = ["-i", "--buffer-sec", "1", "--resume-prompt", "続けて", "初回", "--", "--model", "opus"]
        proc = subprocess.run(
            [sys.executable, CCW_PATH] + args, cwd=self.work, env=self.env, stdin=subprocess.DEVNULL,
            capture_output=True, timeout=60, shell=False,
        )
        stderr = proc.stderr.decode("utf-8", "replace")
        self.assertEqual(proc.returncode, 0, stderr)
        state = self.result()
        self.assertTrue(state["isatty"])
        self.assertEqual(state["argv"], ["--model", "opus", "--", "初回"])
        self.assertEqual(state["received"], "続けて\r")
        self.assertEqual(state["extra"], "")
        stdout = proc.stdout.decode("utf-8", "replace")
        self.assertIn("\x1b[31m5-hour limit reached\x1b[0m", stdout)  # TUI codes passed through
        self.assertIn("resumed ok", stdout)
        self.assertIn("session limit detected", stderr)
        self.assertIn("resuming session (1/5)", stderr)
        self.assertFalse(os.path.exists(os.path.join(self.work, ".ccw.lock")))

    def test_keyboard_raw_mode_and_window_size(self):
        import fcntl
        import pty
        import select
        import struct
        import termios

        master, slave = pty.openpty()
        self.addCleanup(os.close, master)
        self.addCleanup(os.close, slave)
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 30, 100, 0, 0))
        before = termios.tcgetattr(slave)
        errfile = tempfile.TemporaryFile()  # noqa: SIM115 -- closed by addCleanup
        self.addCleanup(errfile.close)
        proc = subprocess.Popen(
            # --buffer-sec 2: the keys below are typed before the resume is due.
            [sys.executable, CCW_PATH, "-i", "--buffer-sec", "2", "--resume-prompt", "go"],
            cwd=self.work, env=self.env, stdin=slave, stdout=subprocess.PIPE, stderr=errfile, shell=False,
        )
        self.addCleanup(proc.stdout.close)
        self.addCleanup(lambda: proc.poll() is None and proc.kill())
        output = b""
        deadline = time.monotonic() + 30
        while b"Welcome" not in output and time.monotonic() < deadline:
            ready, _, _ = select.select([proc.stdout], [], [], 1)
            if ready:
                output += os.read(proc.stdout.fileno(), 4096)
        self.assertIn(b"Welcome", output)
        # The user types during the wait; the resume prompt waits for input to go idle.
        os.write(master, b"hi\x1b[A")
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 40, 120, 0, 0))
        proc.send_signal(signal.SIGWINCH)
        output += proc.stdout.read()
        rc = proc.wait(timeout=30)
        errfile.seek(0)
        self.assertEqual(rc, 0, errfile.read().decode("utf-8", "replace"))
        state = self.result()
        self.assertEqual(state["received"], "hi\x1b[Ago\r")
        self.assertEqual(state["extra"], "")
        self.assertEqual(state["first_size"], [30, 100])
        self.assertEqual(state["last_size"], [40, 120])
        self.assertEqual(termios.tcgetattr(slave), before)  # raw mode undone
        self.assertFalse(os.path.exists(os.path.join(self.work, ".ccw.lock")))


class TestSourceHygiene(unittest.TestCase):
    def test_ccw_is_lf_utf8_without_bom(self):
        with open(CCW_PATH, "rb") as fh:
            data = fh.read()
        self.assertFalse(data.startswith(b"\xef\xbb\xbf"))
        self.assertNotIn(b"\r\n", data)
        self.assertTrue(data.startswith(b"#!/usr/bin/env python3\n"))
        data.decode("utf-8")

    def test_no_shell_true(self):
        with open(CCW_PATH, encoding="utf-8") as fh:
            source = fh.read()
        self.assertNotIn("shell=True", source)


if __name__ == "__main__":
    unittest.main()
