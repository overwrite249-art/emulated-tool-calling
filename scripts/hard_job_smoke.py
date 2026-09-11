#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Opt-in hard-job test: a real coding CLI -> emutools -> a text-only model.

scripts/live_cli_smoke.py proves that one call of each kind survives the
round trip. This fixture is a sustained job instead, and it is deliberately
built out of the things that break emulated tool calling rather than the
things that are hard for a large model:

* an MCP tool result that has to be carried into a later file write,
* a spec file whose payload contains a literal ``</tool_call>``, ``<arg>``
  tags, DeepSeek DSML markers, nested JSON escapes, Windows backslashes that
  double as Python escapes, emoji and triple quotes, which the model must
  read through one tool result and re-emit through another tool argument,
* three ordinary bugs that force several read/edit/run rounds,
* a test the model has to run itself and recover from.

    export EMU_UPSTREAM_API_KEY=sk-...
    python3 scripts/hard_job_smoke.py --cli "$(command -v claude)" \
        --out-dir /tmp/emutools-hard --model deepseek-flash

Never run this in a valuable working directory: it builds its own fixture.
The API key is given only to the proxy, never to the CLI or the MCP process.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import time
import urllib.request

PAYLOAD_LINES = [
    '<tool_call> is not a call here and neither is </tool_call>',
    '<arg name="file_path">/not/a/real/arg</arg>',
    '\uff5c\uff5cDSML\uff5c\uff5c \uff5ctool\u2581calls\u2581begin\uff5c \uff5ctool\u2581call\u2581end\uff5c',
    '{"nested": {"quote": "\\"", "backslash": "\\\\", "n": 3}}',
    'C:\\temp\\new\\files is a Windows path, not escapes',
    '\u041f\u0440\u0438\u0432\u0456\u0442 \U0001f408 \u2014 \u044e\u043d\u0456\u043a\u043e\u0434 \u0442\u0430 \u0435\u043c\u043e\u0434\u0437\u0456',
    "triple quotes: ''' and \"\"\" and `backticks`",
]
PAYLOAD = "\n".join(PAYLOAD_LINES)

MCP_SERVER = r'''import json,sys
from pathlib import Path
TOKEN="RT-7f3a91-\u0416"
for line in sys.stdin:
    try:
        msg=json.loads(line)
        method=msg.get("method")
        if "id" not in msg:
            continue
        if method=="initialize":
            result={"protocolVersion":msg.get("params",{}).get("protocolVersion","2024-11-05"),"capabilities":{"tools":{}},"serverInfo":{"name":"emutools-hard","version":"1.0"}}
        elif method=="tools/list":
            result={"tools":[{"name":"release_token","description":"Return the release token that payload.py must embed.","inputSchema":{"type":"object","properties":{"project":{"type":"string","description":"Project name, always 'chunker'."}},"required":["project"],"additionalProperties":False}}]}
        elif method=="tools/call":
            p=msg.get("params",{})
            a=p.get("arguments",{})
            with Path(__file__).with_name("mcp-events.jsonl").open("a") as log:log.write(json.dumps({"tool":p.get("name"),"arguments":a},ensure_ascii=False)+"\n")
            result={"content":[{"type":"text","text":TOKEN}],"isError":False}
        elif method=="ping":result={}
        else:
            print(json.dumps({"jsonrpc":"2.0","id":msg["id"],"error":{"code":-32601,"message":"Method not found"}}),flush=True)
            continue
        print(json.dumps({"jsonrpc":"2.0","id":msg["id"],"result":result},ensure_ascii=False),flush=True)
    except Exception as e:
        print(str(e),file=sys.stderr,flush=True)
'''
MCP_TOKEN = "RT-7f3a91-\u0416"

BUGGY_MODULE = '''# -*- coding: utf-8 -*-
"""Tiny helpers used by the release tool. Three of these are wrong."""


def chunk(items, size):
    """Split items into consecutive lists of at most `size` elements."""
    out = []
    index = 0
    while index < len(items):
        out.append(items[index:index + size - 1])
        index += size
    return out


def escape(text):
    """Escape a string so it can sit inside a double-quoted JSON string."""
    text = text.replace('"', '\\\\"')
    text = text.replace('\\\\', '\\\\\\\\')
    return text


def width(text):
    """Number of characters in text, counting an emoji as one character."""
    return len(text.encode("utf-8"))
'''


PAYLOAD_STUB = """# -*- coding: utf-8 -*-
# TODO: fill both values in as SPEC.md requires.
PAYLOAD = ""
TOKEN = ""
"""


def spec_text():
    body = "\n".join(PAYLOAD_LINES)
    return (
        "# Release payload spec\n\n"
        "`payload.py` next to `chunker.py` is a stub. It must define exactly two\n"
        "module-level names:\n\n"
        "* `PAYLOAD` - a string equal to the seven lines between the markers\n"
        "  below, joined with a single newline, with no trailing newline. Copy\n"
        "  them character for character. Mind Python escapes: the backslashes\n"
        "  in the Windows path are literal backslashes.\n"
        "* `TOKEN` - the string returned by the `release_token` MCP tool for\n"
        "  project `chunker`.\n\n"
        "--- BEGIN PAYLOAD ---\n" + body + "\n--- END PAYLOAD ---\n\n"
        "`chunker.width` must also count an emoji as one character.\n"
    )


def test_script():
    return (
        '# -*- coding: utf-8 -*-\n'
        '"""Do not modify this file. Fix the project until every check passes."""\n'
        'import json\n'
        'import chunker\n'
        'import payload\n\n'
        'EXPECTED_PAYLOAD = json.loads(' + json.dumps(json.dumps(PAYLOAD)) + ')\n'
        'EXPECTED_TOKEN = json.loads(' + json.dumps(json.dumps(MCP_TOKEN)) + ')\n\n\n'
        'def check(name, got, want):\n'
        '    if got != want:\n'
        '        lines = ["FAIL " + name]\n'
        '        if isinstance(got, str) and isinstance(want, str) and "\\n" in want:\n'
        '            g, w = got.split("\\n"), want.split("\\n")\n'
        '            if len(g) != len(w):\n'
        '                lines.append("  got %d lines, want %d" % (len(g), len(w)))\n'
        '            for i in range(max(len(g), len(w))):\n'
        '                a = g[i] if i < len(g) else "<missing>"\n'
        '                b = w[i] if i < len(w) else "<missing>"\n'
        '                if a != b:\n'
        '                    lines.append("  line %d" % (i + 1))\n'
        '                    lines.append("    got  %r" % (a,))\n'
        '                    lines.append("    want %r" % (b,))\n'
        '        else:\n'
        '            lines.append("  got  %r" % (got,))\n'
        '            lines.append("  want %r" % (want,))\n'
        '        raise SystemExit("\\n".join(lines))\n'
        '    print("ok " + name)\n\n\n'
        'check("chunk exact", chunker.chunk([1, 2, 3, 4, 5, 6], 2), [[1, 2], [3, 4], [5, 6]])\n'
        'check("chunk remainder", chunker.chunk([1, 2, 3, 4, 5], 2), [[1, 2], [3, 4], [5]])\n'
        'check("chunk size three", chunker.chunk(list(range(7)), 3), [[0, 1, 2], [3, 4, 5], [6]])\n'
        'check("escape quote", chunker.escape(\'say "hi"\'), \'say \\\\"hi\\\\"\')\n'
        'check("escape backslash", chunker.escape("C:\\\\tmp"), "C:\\\\\\\\tmp")\n'
        'check("escape both", chunker.escape(\'a\\\\"b\'), \'a\\\\\\\\\\\\"b\')\n'
        'check("width ascii", chunker.width("abc"), 3)\n'
        'check("width emoji", chunker.width("a\\U0001f408b"), 3)\n'
        'check("payload token", payload.TOKEN, EXPECTED_TOKEN)\n'
        'check("payload text", payload.PAYLOAD, EXPECTED_PAYLOAD)\n'
        'print("HARD_TESTS_PASS")\n'
    )


PROMPT = (
    "Read SPEC.md, then finish this release task in the current directory:\n"
    "1. Call the release_token MCP tool for project chunker.\n"
    "2. Fill in payload.py exactly as SPEC.md requires.\n"
    "3. chunker.py has three bugs; fix chunker.py.\n"
    "4. Run `python3 test_hard.py` and keep working until it really prints "
    "HARD_TESTS_PASS.\n"
    "Never modify test_hard.py. Read the failure output carefully; it shows the "
    "exact expected values. When the test truly passes, reply with HARD_JOB_OK."
)

SYSTEM_PROMPT = (
    "You are finishing a small Python release task in the current directory. "
    "Use the Read, Write, Edit, Bash and MCP tools to do real work; never just "
    "describe a patch. Run `python3 test_hard.py` to check your work and read its "
    "failure output carefully before editing again. Keep replies short."
)


def stop(proc):
    if proc and proc.poll() is None:
        os.killpg(proc.pid, signal.SIGTERM)
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.wait(timeout=5)


def balance(key):
    req = urllib.request.Request(
        "https://api.deepseek.com/user/balance", headers={"Authorization": "Bearer " + key})
    try:
        with urllib.request.urlopen(req, timeout=15) as response:
            infos = json.load(response).get("balance_infos") or []
        return float(infos[0]["total_balance"]) if infos else None
    except Exception:
        return None


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--cli", required=True, help="path to the claude binary")
    ap.add_argument("--out-dir", required=True, help="new, disposable directory")
    ap.add_argument("--model", default="deepseek-flash", help="upstream model id")
    ap.add_argument("--timeout", type=int, default=1200)
    ap.add_argument("--max-turns", type=int, default=60)
    ap.add_argument("--tool-rounds", type=int, default=40)
    ap.add_argument("--parallel", action="store_true", help="allow parallel tool calls")
    ap.add_argument("--debug-bodies", action="store_true", help="dump prompts/replies to proxy.log")
    args = ap.parse_args()

    root = Path(__file__).resolve().parents[1]
    key = os.environ.get("EMU_UPSTREAM_API_KEY") or os.environ.get("DEEPSEEK_API_KEY")
    if not key:
        ap.error("set EMU_UPSTREAM_API_KEY; this test makes paid API requests")

    work = Path(args.out_dir).resolve()
    work.mkdir(parents=True, exist_ok=False)
    fixture = work / "fixture"
    fixture.mkdir()
    (fixture / "SPEC.md").write_text(spec_text(), encoding="utf-8")
    (fixture / "chunker.py").write_text(BUGGY_MODULE, encoding="utf-8")
    (fixture / "payload.py").write_text(PAYLOAD_STUB, encoding="utf-8")
    (fixture / "hard_mcp.py").write_text(MCP_SERVER, encoding="utf-8")
    test_path = fixture / "test_hard.py"
    test_path.write_text(test_script(), encoding="utf-8")
    test_digest = hashlib.sha256(test_path.read_bytes()).hexdigest()

    home = work / "home"
    home.mkdir()
    port = free_port()
    url = "http://127.0.0.1:%d" % port

    env = dict(os.environ)
    for name in list(env):
        if name.endswith(("API_KEY", "AUTH_TOKEN")) or name in ("ANTHROPIC_BASE_URL", "OPENAI_BASE_URL"):
            env.pop(name, None)
    env.update(HOME=str(home), XDG_CONFIG_HOME=str(home / ".config"),
               XDG_DATA_HOME=str(home / ".local/share"), XDG_CACHE_HOME=str(home / ".cache"),
               DISABLE_TELEMETRY="1", DO_NOT_TRACK="1")

    proxy_env = dict(env, EMU_UPSTREAM_API_KEY=key, EMU_HOST="127.0.0.1", EMU_PORT=str(port),
                     EMU_MODEL_BIG=args.model, EMU_MODEL_SMALL=args.model,
                     EMU_MAX_TOOL_ROUNDS=str(args.tool_rounds), EMU_USE_STOP="0",
                     EMU_PARALLEL="1" if args.parallel else "0",
                     EMU_LOG="debug", EMU_LOG_BODIES="1" if args.debug_bodies else "0",
                     EMU_TIMEOUT="180")
    client_env = dict(env, ANTHROPIC_BASE_URL=url, ANTHROPIC_API_KEY="dummy",
                      ANTHROPIC_AUTH_TOKEN="dummy",
                      CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC="1",
                      CLAUDE_CODE_MAX_OUTPUT_TOKENS="8192", MAX_THINKING_TOKENS="0")

    mcp = {"mcpServers": {"release": {"command": sys.executable,
                                      "args": [str(fixture / "hard_mcp.py")],
                                      "cwd": str(fixture)}}}
    config_path = work / "mcp.json"
    config_path.write_text(json.dumps(mcp), encoding="utf-8")

    command = [args.cli, "--bare", "--print", "--model", "claude-sonnet-4-5",
               "--output-format", "stream-json", "--verbose", "--no-session-persistence",
               "--permission-mode", "dontAsk", "--strict-mcp-config",
               "--mcp-config", str(config_path),
               "--tools", "Read,Edit,Write,Bash",
               "--allowedTools",
               "Read,Edit,Write,Bash(python3 test_hard.py),mcp__release__release_token",
               "--max-turns", str(args.max_turns),
               "--system-prompt", SYSTEM_PROMPT, PROMPT]

    proxy = client = None
    started = time.monotonic()
    before = balance(key)
    result = {"model": args.model, "parallel": bool(args.parallel), "balance_before_cny": before}
    try:
        with (work / "proxy.log").open("w") as proxy_log:
            proxy = subprocess.Popen([sys.executable, "-m", "emutools"], cwd=root, env=proxy_env,
                                     stdout=proxy_log, stderr=subprocess.STDOUT, start_new_session=True)
            for _ in range(100):
                try:
                    with urllib.request.urlopen(url + "/health", timeout=1) as response:
                        json.load(response)
                    break
                except Exception:
                    if proxy.poll() is not None:
                        raise RuntimeError("proxy failed to start; see proxy.log")
                    time.sleep(.1)
            else:
                raise RuntimeError("proxy startup timed out")

            version = subprocess.run([args.cli, "--version"], env=client_env,
                                     capture_output=True, text=True, timeout=30)
            result["client_version"] = version.stdout.strip()

            with (work / "client.jsonl").open("w") as out, (work / "client.stderr").open("w") as err:
                client = subprocess.Popen(command, cwd=fixture, env=client_env,
                                          stdout=out, stderr=err, start_new_session=True)
                try:
                    result["exit_code"] = client.wait(timeout=args.timeout)
                except subprocess.TimeoutExpired:
                    result["timeout"] = True
                    stop(client)
                    result["exit_code"] = client.returncode

            check = subprocess.run([sys.executable, "test_hard.py"], cwd=fixture,
                                   capture_output=True, text=True, timeout=30)
            result["independent_test_passed"] = (
                check.returncode == 0 and "HARD_TESTS_PASS" in check.stdout)
            result["independent_test_output"] = (check.stdout + check.stderr).strip()[-1500:]
            result["test_file_untouched"] = (
                hashlib.sha256(test_path.read_bytes()).hexdigest() == test_digest)

            events = fixture / "mcp-events.jsonl"
            calls = []
            if events.exists():
                for line in events.read_text(encoding="utf-8").splitlines():
                    try:
                        calls.append(json.loads(line))
                    except ValueError:
                        pass
            result["mcp_calls"] = calls
            result["mcp_called"] = any(c.get("tool") == "release_token" for c in calls)

            transcript = (work / "client.jsonl").read_text(encoding="utf-8", errors="replace")
            records = []
            for line in transcript.splitlines():
                try:
                    records.append(json.loads(line))
                except ValueError:
                    pass
            result["client_ran_real_test"] = "HARD_TESTS_PASS" in transcript
            result["client_reported_success"] = any(
                r.get("type") == "result" and not r.get("is_error")
                and "HARD_JOB_OK" in (r.get("result") or "") for r in records)
            result["tool_uses"] = [
                block.get("name") for r in records if r.get("type") == "assistant"
                for block in (r.get("message", {}).get("content") or [])
                if isinstance(block, dict) and block.get("type") == "tool_use"]
            result["tool_use_count"] = len(result["tool_uses"])
            final = [r for r in records if r.get("type") == "result"]
            if final:
                result["final_result_excerpt"] = json.dumps(final[-1], ensure_ascii=False)[:1200]
            result["passed"] = bool(
                result["exit_code"] == 0 and result["independent_test_passed"]
                and result["test_file_untouched"] and result["mcp_called"]
                and result["client_ran_real_test"] and result["client_reported_success"])
    finally:
        stop(client)
        stop(proxy)
        after = balance(key)
        result["balance_after_cny"] = after
        if before is not None and after is not None:
            result["spent_cny"] = round(before - after, 4)
        result["elapsed_seconds"] = round(time.monotonic() - started, 2)
        (work / "result.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("passed") else 1


if __name__ == "__main__":
    sys.exit(main())
