# --- generated header: build_single_file.py strips these blocks ---
from __future__ import annotations
from ._prelude import *  # noqa: F401,F403
# --- end generated header ---


# ======================================================================================
# Config
# ======================================================================================


def _env_warn(msg: str) -> None:
    # Config is built before the logger exists, so warn straight to stderr.
    sys.stderr.write("emutools: warning: %s\n" % msg)


def _env(name: str, default: str = "") -> str:
    v = os.environ.get(name)
    if v is None or v.strip() == "":
        return default
    return v.strip()


def _env_int(name: str, default: int) -> int:
    try:
        raw = os.environ.get(name)
        if raw is None or raw.strip() == "":
            return default
        return int(raw.strip())
    except (TypeError, ValueError):
        _env_warn("%s=%r is not an integer; using %s" % (name, raw, default))
        return default


def _env_float(name: str, default: float) -> float:
    try:
        raw = os.environ.get(name)
        if raw is None or raw.strip() == "":
            return default
        v = float(raw.strip())
    except (TypeError, ValueError):
        _env_warn("%s=%r is not a number; using %s" % (name, raw, default))
        return default
    if not math.isfinite(v) or v <= 0:
        _env_warn("%s=%r is not a positive finite number; using %s" % (name, raw, default))
        return default
    return v


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    low = raw.strip().lower()
    if low in ("1", "true", "yes", "on", "y", "enabled", "enable"):
        return True
    if low in ("0", "false", "no", "off", "n", "disabled", "disable"):
        return False
    _env_warn("%s=%r is not a boolean; using %s" % (name, raw, default))
    return default


_LOG_LEVEL_NAMES = ("debug", "info", "warn", "error", "silent")


def _normalize_log_level(raw: str) -> str:
    low = (raw or "").strip().lower()
    low = {"warning": "warn", "err": "error", "quiet": "silent", "none": "silent"}.get(low, low)
    if low not in _LOG_LEVEL_NAMES:
        _env_warn("EMU_LOG=%r is not one of %s; using info" % (raw, "/".join(_LOG_LEVEL_NAMES)))
        return "info"
    return low


@dataclass
class Config:
    host: str = field(default_factory=lambda: _env("EMU_HOST", "127.0.0.1"))
    port: int = field(default_factory=lambda: _env_int("EMU_PORT", 8787))

    upstream_base: str = field(
        default_factory=lambda: _env("EMU_UPSTREAM_BASE_URL", "https://api.deepseek.com").rstrip("/")
    )
    upstream_key: str = field(
        default_factory=lambda: _env("EMU_UPSTREAM_API_KEY", _env("DEEPSEEK_API_KEY", ""))
    )
    upstream_path: str = field(default_factory=lambda: _env("EMU_UPSTREAM_PATH", "/chat/completions"))

    # Model routing. Clients send e.g. "claude-sonnet-4-5"; we map onto upstream ids.
    model_big: str = field(default_factory=lambda: _env("EMU_MODEL_BIG", "deepseek-v4-pro"))
    model_small: str = field(default_factory=lambda: _env("EMU_MODEL_SMALL", "deepseek-flash"))
    model_map_raw: str = field(default_factory=lambda: _env("EMU_MODEL_MAP", ""))

    # Loop / runaway protection
    max_tool_rounds: int = field(default_factory=lambda: _env_int("EMU_MAX_TOOL_ROUNDS", 25))
    max_repeat: int = field(default_factory=lambda: _env_int("EMU_MAX_REPEAT", 3))
    max_calls_per_turn: int = field(default_factory=lambda: _env_int("EMU_MAX_CALLS_PER_TURN", 4))
    loop_retry: bool = field(default_factory=lambda: _env_bool("EMU_LOOP_RETRY", True))

    # Emulation behaviour
    parallel: bool = field(default_factory=lambda: _env_bool("EMU_PARALLEL", False))
    use_stop: bool = field(default_factory=lambda: _env_bool("EMU_USE_STOP", False))
    merge_roles: bool = field(default_factory=lambda: _env_bool("EMU_MERGE_ROLES", True))
    salvage_bare_json: bool = field(default_factory=lambda: _env_bool("EMU_SALVAGE", True))
    max_result_chars: int = field(default_factory=lambda: _env_int("EMU_MAX_RESULT_CHARS", 24000))

    # Inbound HTTP resource limits (the server is intended for loopback use).
    max_request_bytes: int = field(default_factory=lambda: _env_int("EMU_MAX_REQUEST_BYTES", 16 * 1024 * 1024))
    client_timeout: float = field(default_factory=lambda: _env_float("EMU_CLIENT_TIMEOUT", 30.0))

    # Upstream transport
    timeout: float = field(default_factory=lambda: _env_float("EMU_TIMEOUT", 300.0))
    connect_retries: int = field(default_factory=lambda: _env_int("EMU_MAX_RETRIES", 3))

    # Browser origins allowed to call the proxy (comma-separated; "*" for any).
    # Loopback origins are always allowed. Everything else is refused so a web page
    # cannot spend the upstream key through the local proxy.
    cors_origins: str = field(default_factory=lambda: _env("EMU_CORS_ORIGINS", ""))

    log_level: str = field(default_factory=lambda: _normalize_log_level(_env("EMU_LOG", "info")))
    log_bodies: bool = field(default_factory=lambda: _env_bool("EMU_LOG_BODIES", False))

    def model_map(self) -> Dict[str, str]:
        raw = self.model_map_raw
        cache = self.__dict__.get("_model_map_cache")
        if cache is not None and cache[0] == raw:
            return dict(cache[1])
        parsed = self._parse_model_map()
        self.__dict__["_model_map_cache"] = (raw, parsed)
        return dict(parsed)

    def _parse_model_map(self) -> Dict[str, str]:
        if not self.model_map_raw.strip():
            return {}
        raw = self.model_map_raw.strip()
        if not raw.startswith("{"):
            mapping: Dict[str, str] = {}
            for item in raw.split(","):
                if not item.strip():
                    continue
                key, sep, value = item.partition("=")
                if not sep or not key.strip() or not value.strip():
                    log_warn("EMU_MODEL_MAP must be JSON or comma-separated from=to pairs; ignoring")
                    return {}
                mapping[key.strip()] = value.strip()
            return mapping
        try:
            data = json.loads(raw)
            if isinstance(data, dict):
                return {k: v for k, v in data.items() if k and isinstance(v, str) and v}
        except (ValueError, TypeError):
            log_warn("EMU_MODEL_MAP is not valid JSON; ignoring")
        return {}

    def resolve_model(self, requested: str) -> str:
        requested = (requested or "").strip()
        mapping = self.model_map()
        if requested in mapping:
            return mapping[requested]
        low = requested.lower()
        # Most specific (longest) key wins for partial matches.
        for key in sorted(mapping, key=len, reverse=True):
            if key.lower() in low:
                return mapping[key]
        # Already an upstream id? pass through untouched.
        if low.startswith("deepseek") or low.startswith("qwen") or low.startswith("glm"):
            return requested
        # Small/fast tier used by Claude Code for titles & cheap classification.
        # Whole tokens only: "mini" must not match inside "gemini" or "minimax".
        toks = set(t for t in re.split(r"[^a-z0-9]+", low) if t)
        if toks & {"haiku", "mini", "small", "flash", "fast", "lite"}:
            return self.model_small
        return self.model_big


CFG = Config()

_LEVELS = {"debug": 10, "info": 20, "warn": 30, "error": 40, "silent": 100}
_LOG_LOCK = threading.Lock()


_LOG_LOCAL = threading.local()


def set_thread_log_level(level: Optional[str]) -> None:
    """Let a server thread log at its own Config's level (None = global CFG)."""
    _LOG_LOCAL.level = level


def _log(level: str, msg: str) -> None:
    current = getattr(_LOG_LOCAL, "level", None) or CFG.log_level
    if _LEVELS.get(level, 20) < _LEVELS.get(current, 20):
        return
    line = "%s [%s] %s" % (time.strftime("%H:%M:%S"), level.upper()[:4], msg)
    with _LOG_LOCK:
        sys.stderr.write(line + "\n")
        sys.stderr.flush()


def log_debug(m: str) -> None:
    _log("debug", m)


def log_info(m: str) -> None:
    _log("info", m)


def log_warn(m: str) -> None:
    _log("warn", m)


def log_error(m: str) -> None:
    _log("error", m)


# ======================================================================================
# Small utilities
# ======================================================================================

_ID_ALPHABET = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"


def _rand_id(n: int = 24) -> str:
    return "".join(random.choice(_ID_ALPHABET) for _ in range(n))


def new_tool_use_id() -> str:
    """Anthropic-shaped tool_use id."""
    return "toolu_" + _rand_id(24)


def new_openai_call_id() -> str:
    return "call_" + _rand_id(24)


def new_message_id(prefix: str = "msg") -> str:
    return prefix + "_" + _rand_id(24)


def canon_json(obj: Any) -> str:
    try:
        return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    except (TypeError, ValueError):
        return repr(obj)


def fingerprint(name: str, args: Any) -> str:
    raw = (name or "") + "\x00" + canon_json(args)
    return hashlib.sha1(raw.encode("utf-8", "replace")).hexdigest()[:16]


def estimate_tokens(text: str) -> int:
    """Cheap but stable token estimate (~4 chars/token, min 1 per non-empty)."""
    if not text:
        return 0
    return max(1, int(len(text) / 3.7))


def truncate_middle(text: str, limit: int) -> str:
    if limit <= 0 or len(text) <= limit:
        return text
    marker_len = len("\n... [%d characters omitted] ...\n" % len(text))
    if limit <= marker_len:
        return text[:limit]
    budget = limit - marker_len
    head = budget // 2
    tail = budget - head
    omitted = len(text) - head - tail
    return text[:head] + ("\n... [%d characters omitted] ...\n" % omitted) + (text[-tail:] if tail else "")


def utf8(text: str) -> bytes:
    """UTF-8 encode, repairing lone surrogates (e.g. from JSON "\\ud83d") instead of raising."""
    try:
        return text.encode("utf-8")
    except UnicodeEncodeError:
        fixed = text.encode("utf-16", "surrogatepass").decode("utf-16", "replace")
        return fixed.encode("utf-8", "replace")


def safe_str(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, str):
        return v
    if isinstance(v, (dict, list)):
        try:
            return json.dumps(v, ensure_ascii=False)
        except (TypeError, ValueError):
            return str(v)
    return str(v)


# ======================================================================================
# Canonical types
# ======================================================================================


@dataclass
class ToolDef:
    name: str
    description: str = ""
    schema: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ToolCall:
    name: str
    args: Dict[str, Any]
    id: str = ""
    raw: str = ""
    repaired: bool = False

    def fp(self) -> str:
        return fingerprint(self.name, self.args)


@dataclass
class CanonMessage:
    """Protocol-neutral message."""

    role: str  # system | user | assistant
    text: str = ""
    tool_calls: List[ToolCall] = field(default_factory=list)
    # tool results attached to a user turn: (tool_use_id, name, content, is_error)
    tool_results: List[Tuple[str, str, str, bool]] = field(default_factory=list)


@dataclass
class CanonRequest:
    model: str
    messages: List[CanonMessage]
    system: str = ""
    tools: List[ToolDef] = field(default_factory=list)
    tool_choice: str = "auto"  # auto | required | none | <tool name>
    max_tokens: int = 4096
    temperature: Optional[float] = None
    top_p: Optional[float] = None
    stop: List[str] = field(default_factory=list)
    stream: bool = False
    parallel_tool_calls: Optional[bool] = None
    protocol: str = "anthropic"  # anthropic | openai


# ======================================================================================
# Prompt construction (this is what replaces native tool calling)
# ======================================================================================

CALL_OPEN = "<tool_call>"
CALL_CLOSE = "</tool_call>"
RESULT_OPEN = "<tool_result>"
RESULT_CLOSE = "</tool_result>"


# --- generated header: build_single_file.py strips these blocks ---
__all__ = [
    "_env",
    "_env_int",
    "_env_float",
    "_env_bool",
    "Config",
    "CFG",
    "_LEVELS",
    "_LOG_LOCK",
    "_log",
    "log_debug",
    "log_info",
    "log_warn",
    "log_error",
    "_ID_ALPHABET",
    "_rand_id",
    "new_tool_use_id",
    "new_openai_call_id",
    "new_message_id",
    "canon_json",
    "fingerprint",
    "estimate_tokens",
    "truncate_middle",
    "safe_str",
    "utf8",
    "set_thread_log_level",
    "ToolDef",
    "ToolCall",
    "CanonMessage",
    "CanonRequest",
    "CALL_OPEN",
    "CALL_CLOSE",
    "RESULT_OPEN",
    "RESULT_CLOSE",
]
# --- end generated header ---
