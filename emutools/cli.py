# --- generated header: build_single_file.py strips these blocks ---
from __future__ import annotations
from ._prelude import *  # noqa: F401,F403
from .core import *  # noqa: F401,F403
from .protocol import *  # noqa: F401,F403
from .wire import *  # noqa: F401,F403
from .engine import *  # noqa: F401,F403
from .server import *  # noqa: F401,F403
from .selftest_a import *  # noqa: F401,F403
from .selftest_b import *  # noqa: F401,F403
# --- end generated header ---


# ======================================================================================
# Entry point
# ======================================================================================


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="emutools",
        description="Emulated tool-calling proxy for Claude Code and OpenAI-compatible clients.",
    )
    parser.add_argument("--host", default=None, help="bind host (default 127.0.0.1)")
    parser.add_argument("--port", type=int, default=None, help="bind port (default 8787)")
    parser.add_argument("--upstream", default=None, help="upstream base URL")
    parser.add_argument("--api-key", default=None, help="upstream API key")
    parser.add_argument("--model-big", default=None, help="model for the main tier")
    parser.add_argument("--model-small", default=None, help="model for the fast tier")
    parser.add_argument("--max-tool-rounds", type=int, default=None)
    parser.add_argument("--max-repeat", type=int, default=None)
    parser.add_argument("--parallel", action="store_true", help="allow multiple calls per reply")
    parser.add_argument("--log", default=None, choices=["debug", "info", "warn", "error", "silent"])
    parser.add_argument("--log-bodies", action="store_true")
    parser.add_argument("--selftest", action="store_true", help="run the offline test suite")
    parser.add_argument("--version", action="version", version="emutools " + __version__)
    args = parser.parse_args(argv)

    if args.selftest:
        CFG.log_level = args.log or "error"
        return run_selftest()

    if args.host:
        CFG.host = args.host
    if args.port:
        CFG.port = args.port
    if args.upstream:
        CFG.upstream_base = args.upstream.rstrip("/")
    if args.api_key:
        CFG.upstream_key = args.api_key
    if args.model_big:
        CFG.model_big = args.model_big
    if args.model_small:
        CFG.model_small = args.model_small
    if args.max_tool_rounds is not None:
        CFG.max_tool_rounds = args.max_tool_rounds
    if args.max_repeat is not None:
        CFG.max_repeat = args.max_repeat
    if args.parallel:
        CFG.parallel = True
    if args.log:
        CFG.log_level = args.log
    if args.log_bodies:
        CFG.log_bodies = True

    serve(CFG)
    return 0


# --- generated header: build_single_file.py strips these blocks ---
__all__ = [
    "main",
]
# --- end generated header ---
