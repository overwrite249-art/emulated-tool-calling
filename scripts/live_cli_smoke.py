#!/usr/bin/env python3
"""Opt-in live test: real CLI -> emutools -> DeepSeek V4 Pro.

Requires an installed Claude Code or OpenCode binary. Live mode also requires
EMU_UPSTREAM_API_KEY; --mock-upstream uses a deterministic local model in CI.
Never run this in a valuable working directory: it creates its own fixture.
The API key is passed only to the proxy, never to the CLI or fixture MCP server.
"""
import argparse
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import time
import urllib.request


MCP_SERVER = r'''import json,sys
from pathlib import Path
for line in sys.stdin:
    try:
        msg=json.loads(line)
        method=msg.get("method")
        if "id" not in msg:
            continue
        if method=="initialize":
            result={"protocolVersion":msg.get("params",{}).get("protocolVersion","2024-11-05"),"capabilities":{"tools":{}},"serverInfo":{"name":"emutools-smoke","version":"1.0"}}
        elif method=="tools/list":
            result={"tools":[{"name":"sum","description":"Add two integers for the integration smoke test.","inputSchema":{"type":"object","properties":{"a":{"type":"integer"},"b":{"type":"integer"}},"required":["a","b"],"additionalProperties":False}}]}
        elif method=="tools/call":
            p=msg.get("params",{})
            a=p.get("arguments",{})
            value=a["a"]+a["b"]
            with Path(__file__).with_name("mcp-events.jsonl").open("a") as log:log.write(json.dumps({"tool":p["name"],"arguments":a,"result":value})+"\n")
            result={"content":[{"type":"text","text":str(value)}],"isError":False}
        elif method=="ping":result={}
        else:
            print(json.dumps({"jsonrpc":"2.0","id":msg["id"],"error":{"code":-32601,"message":"Method not found"}}),flush=True)
            continue
        print(json.dumps({"jsonrpc":"2.0","id":msg["id"],"result":result}),flush=True)
    except Exception as e:
        print(str(e),file=sys.stderr,flush=True)
'''


def stop(proc):
    if proc and proc.poll() is None:
        os.killpg(proc.pid, signal.SIGTERM)
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.wait(timeout=5)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--client", choices=["claude", "opencode"], required=True)
    ap.add_argument("--cli", required=True)
    ap.add_argument("--out-dir", required=True, help="new, disposable directory for fixture and logs")
    ap.add_argument("--timeout", type=int, default=180)
    ap.add_argument("--mock-upstream", action="store_true", help="no paid model; actual CLI/tools still run")
    args = ap.parse_args()
    root = Path(__file__).resolve().parents[1]
    key = "mock" if args.mock_upstream else os.environ.get("EMU_UPSTREAM_API_KEY") or os.environ.get("DEEPSEEK_API_KEY")
    if not key:
        ap.error("set EMU_UPSTREAM_API_KEY; the test makes paid API requests")
    work = Path(args.out_dir).resolve()
    work.mkdir(parents=True, exist_ok=False)
    fixture = work / "fixture"
    fixture.mkdir()
    (fixture / "greeting.py").write_text('def greeting():\n    return "old"\n', encoding="utf-8")
    expected = "Привіт 🐈 </tool_call>"
    (fixture / "test_greeting.py").write_text(
        'from greeting import greeting\nassert greeting() == ' + repr(expected) + '\nprint("FIXTURE_TEST_PASS")\n', encoding="utf-8")
    (fixture / "smoke_mcp.py").write_text(MCP_SERVER, encoding="utf-8")
    home = work / "home"
    home.mkdir()
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    url = "http://127.0.0.1:%s" % port
    env = dict(os.environ)
    for k in list(env):
        if k.endswith(("API_KEY", "AUTH_TOKEN")) or k in ("ANTHROPIC_BASE_URL", "OPENAI_BASE_URL"):
            env.pop(k, None)
    env.update(HOME=str(home), XDG_CONFIG_HOME=str(home / ".config"), XDG_DATA_HOME=str(home / ".local/share"),
               XDG_CACHE_HOME=str(home / ".cache"), DISABLE_TELEMETRY="1", DO_NOT_TRACK="1")
    proxy_env = dict(env, EMU_UPSTREAM_API_KEY=key, EMU_HOST="127.0.0.1", EMU_PORT=str(port),
                     EMU_MODEL_BIG="deepseek-v4-pro", EMU_MODEL_SMALL="deepseek-v4-pro", EMU_MAX_TOOL_ROUNDS="6",
                     EMU_MAX_RETRIES="1", EMU_LOOP_RETRY="0", EMU_USE_STOP="0", EMU_LOG_BODIES="0", EMU_TIMEOUT="60")
    client_env = dict(env, ANTHROPIC_BASE_URL=url, ANTHROPIC_API_KEY="dummy", ANTHROPIC_AUTH_TOKEN="dummy",
                      OPENAI_BASE_URL=url + "/v1", OPENAI_API_KEY="dummy", CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC="1",
                      CLAUDE_CODE_MAX_OUTPUT_TOKENS="1024", MAX_THINKING_TOKENS="0")
    prompt = ('First use the smoke MCP sum tool with a=17 and b=25. Then read greeting.py, edit only its greeting function '
              'to return exactly ' + repr(expected) + ', and run python3 test_greeting.py. '
              'Use the real tools, not a code example. Do not read other directories. '
              'After the real test passes, finish with CLI_SMOKE_OK and the sum result. Keep replies brief.')
    mcp = {"mcpServers": {"smoke": {"command": sys.executable, "args": [str(fixture / "smoke_mcp.py")], "cwd": str(fixture)}}}
    config_path = work / "mcp.json"
    config_path.write_text(json.dumps(mcp), encoding="utf-8")
    if args.client == "claude":
        command = [args.cli, "--bare", "--restricted", "--print", "--model", "claude-sonnet-4-5",
                   "--output-format", "stream-json", "--verbose", "--no-session-persistence",
                   "--permission-mode", "dontAsk", "--strict-mcp-config", "--mcp-config", str(config_path),
                   "--tools", "Read,Edit,Bash", "--allowedTools", "Read,Edit,Bash(python3 test_greeting.py),mcp__smoke__sum",
                   "--max-turns", "8", "--max-budget-usd", "0.25", "--system-prompt",
                   "You are running a tiny coding integration test. Work only in the current fixture directory. Use tools and report actual results. No network access is needed.", prompt]
    else:
        config = {
            "$schema": "https://opencode.ai/config.json", "model": "emutools/deepseek-v4-pro",
            "small_model": "emutools/deepseek-v4-pro", "autoupdate": False, "share": "disabled",
            "provider": {"emutools": {"npm": "@ai-sdk/openai-compatible", "name": "emutools",
                "options": {"baseURL": url + "/v1", "apiKey": "dummy"},
                "models": {"deepseek-v4-pro": {"name": "DeepSeek V4 Pro", "limit": {"context": 65536, "output": 1024}}}}},
            "mcp": {"smoke": {"type": "local", "command": [sys.executable, str(fixture / "smoke_mcp.py")], "enabled": True}},
            "permission": {"*": "deny", "read": "allow", "edit": "allow", "smoke_sum": "allow",
                           "bash": {"*": "deny", "python3 test_greeting.py": "allow"}},
        }
        (fixture / "opencode.json").write_text(json.dumps(config), encoding="utf-8")
        client_env.update(OPENCODE_DISABLE_AUTOUPDATE="true", OPENCODE_DISABLE_MODELS_FETCH="true")
        command = [args.cli, "run", "--model", "emutools/deepseek-v4-pro", "--format", "json", prompt]
    mock = None
    if args.mock_upstream:
        sys.path.insert(0, str(root))
        from cli_mock_upstream import CLIMockUpstream
        mock = CLIMockUpstream(fixture)
        mock_port = mock.start()
        proxy_env["EMU_UPSTREAM_BASE_URL"] = "http://127.0.0.1:%d" % mock_port
    proxy = client = None
    started = time.monotonic()
    result = {"client": args.client, "upstream_model": "deterministic mock" if args.mock_upstream else "deepseek-v4-pro"}
    try:
        with (work / "proxy.log").open("w") as proxy_log:
            proxy = subprocess.Popen([sys.executable, "-m", "emutools"], cwd=root, env=proxy_env,
                                     stdout=proxy_log, stderr=subprocess.STDOUT, start_new_session=True)
            for _ in range(100):
                try:
                    with urllib.request.urlopen(url + "/health", timeout=1) as response:
                        health = json.load(response)
                    assert health["model_big"] == "deepseek-v4-pro"
                    break
                except Exception:
                    if proxy.poll() is not None:
                        raise RuntimeError("proxy failed to start; see proxy.log")
                    time.sleep(.1)
            else:
                raise RuntimeError("proxy startup timed out")
            version = subprocess.run([args.cli, "--version"], env=client_env, capture_output=True, text=True, timeout=20)
            result["version"] = version.stdout.strip()
            with (work / "client.jsonl").open("w") as out, (work / "client.stderr").open("w") as err:
                client = subprocess.Popen(command, cwd=fixture, env=client_env, stdout=out, stderr=err, start_new_session=True)
                try:
                    result["exit_code"] = client.wait(timeout=args.timeout)
                except subprocess.TimeoutExpired:
                    result["timeout"] = True
                    stop(client)
                    result["exit_code"] = client.returncode
            test = subprocess.run([sys.executable, "test_greeting.py"], cwd=fixture, capture_output=True, text=True, timeout=10)
            result["fixture_test_passed"] = test.returncode == 0 and "FIXTURE_TEST_PASS" in test.stdout
            events = fixture / "mcp-events.jsonl"
            mcp_calls = [json.loads(line) for line in events.read_text().splitlines()] if events.exists() else []
            result["mcp_calls"] = mcp_calls
            result["mcp_passed"] = any(x == {"tool": "sum", "arguments": {"a": 17, "b": 25}, "result": 42} for x in mcp_calls)
            transcript = (work / "client.jsonl").read_text()
            records = []
            for line in transcript.splitlines():
                try:
                    records.append(json.loads(line))
                except ValueError:
                    pass
            result["client_reported_success"] = any(
                (r.get("type") == "result" and not r.get("is_error") and "CLI_SMOKE_OK" in r.get("result", "")) or
                (r.get("type") == "text" and "CLI_SMOKE_OK" in r.get("part", {}).get("text", "")) for r in records)
            result["client_ran_fixture_test"] = "FIXTURE_TEST_PASS" in transcript
            result["passed"] = (result["exit_code"] == 0 and result["fixture_test_passed"]
                                and result["mcp_passed"] and result["client_reported_success"] and result["client_ran_fixture_test"])
    finally:
        stop(client)
        stop(proxy)
        if mock:
            mock.stop()
        result["elapsed_seconds"] = round(time.monotonic() - started, 2)
        (work / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("passed") else 1


if __name__ == "__main__":
    sys.exit(main())
