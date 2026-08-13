"""emutools.py - Emulated tool-calling proxy.

Speaks BOTH wire protocols that coding agents use:

  * Anthropic Messages API   POST /v1/messages            (Claude Code CLI)
                             POST /v1/messages/count_tokens
  * OpenAI Chat Completions  POST /v1/chat/completions     (opencode, aider, cline, ...)
                             GET  /v1/models

...and translates them onto ANY OpenAI-compatible upstream (DeepSeek by default)
WITHOUT using the upstream's native tool-calling support.

Tool calls are *emulated*: tool schemas are rendered into the prompt, the model
emits a text block, and this proxy parses that block back into real, native
`tool_use` / `tool_calls` structures so the client never knows the difference.

Zero dependencies. Python 3.9+. Single file.

    export EMU_UPSTREAM_API_KEY=sk-...
    python3 emutools.py

    # Claude Code
    ANTHROPIC_BASE_URL=http://127.0.0.1:8787 ANTHROPIC_AUTH_TOKEN=dummy claude

    # opencode / any OpenAI client
    OPENAI_BASE_URL=http://127.0.0.1:8787/v1 OPENAI_API_KEY=dummy opencode

    # run the full test suite (no network needed)
    python3 emutools.py --selftest"""

from ._prelude import *  # noqa: F401,F403
from .core import *  # noqa: F401,F403
from .protocol import *  # noqa: F401,F403
from .wire import *  # noqa: F401,F403
from .engine import *  # noqa: F401,F403
from .server import *  # noqa: F401,F403
from .selftest_a import *  # noqa: F401,F403
from .selftest_b import *  # noqa: F401,F403
from .cli import *  # noqa: F401,F403

from .cli import main  # noqa: F401
