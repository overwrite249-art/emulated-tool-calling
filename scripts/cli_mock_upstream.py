"""Deterministic upstream for real-CLI CI. No model or API key is involved.

Only the model is mocked: the CLI, HTTP proxy, stdio MCP tool, file edits and
fixture test process are real. Separate opt-in tests exercise DeepSeek itself.
"""
import re
from emutools.selftest_a import _MockUpstream
from emutools.core import ToolCall
from emutools.protocol import render_tool_call_text


class CLIMockUpstream(_MockUpstream):
    def __init__(self, fixture):
        super().__init__()
        self.fixture = fixture

    def _next(self):
        messages = self.last_request().get("messages", [])
        system = "\n".join(m.get("content", "") for m in messages if m.get("role") == "system")
        tools = re.findall(r"^### ([\w.-]+)$", system, re.MULTILINE)
        if not tools:
            return "CLI smoke test"
        results = []
        for message in messages:
            if message.get("role") == "user":
                results.extend(re.findall(r'<tool_result name="([^"]+)"[^>]*>\n(.*?)\n</tool_result>',
                                          message.get("content", ""), re.DOTALL))
        name, output = results[-1] if results else ("", "")
        lower = name.lower()
        if not results:
            selected = next((x for x in tools if x.endswith("sum")), "smoke_sum")
            args = {"a": 17, "b": 25}
        elif lower.endswith("sum"):
            selected = next(x for x in tools if x.lower() == "read")
            args = {"file_path" if selected == "Read" else "filePath": str(self.fixture / "greeting.py")}
        elif lower == "read":
            selected = next(x for x in tools if x.lower() == "edit")
            keys = ("file_path", "old_string", "new_string") if selected == "Edit" else ("filePath", "oldString", "newString")
            args = dict(zip(keys, (str(self.fixture / "greeting.py"), 'return "old"', 'return "Привіт 🐈 </tool_call>"')))
        elif lower == "edit":
            selected = next(x for x in tools if x.lower() == "bash")
            args = {"command": "python3 test_greeting.py", "description": "Run integration fixture test"}
        elif lower == "bash" and "FIXTURE_TEST_PASS" in output:
            return "CLI_SMOKE_OK 42"
        else:
            return "The fixture did not pass; inspect the real tool output."
        return render_tool_call_text(ToolCall(selected, args))
