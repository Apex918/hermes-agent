"""Mem0 memory plugin — MemoryProvider interface.

Server-side LLM fact extraction, semantic search, and automatic deduplication
via the Mem0 Platform API (cloud) or OSS (self-hosted) via Memory.

Original PR #2933 by kartik-mem0, adapted to MemoryProvider ABC.

Configuration
-------------
Secret (lives in $HERMES_HOME/.env or the environment):
  MEM0_API_KEY       — Mem0 Platform API key (required for platform mode)
  MEM0_HOST          — Base URL of a self-hosted Mem0 server. When set, the
                       plugin talks to that server directly over HTTP
                       (X-API-Key auth) instead of the cloud API.

Behavioral settings (live in $HERMES_HOME/mem0.json, set via `hermes memory
setup`):
  mode               — Backend mode: "platform" (default) or "oss"
  host               — Self-hosted Mem0 server URL (alt: MEM0_HOST env var).
                       When set, routes to the self-hosted HTTP backend.
  user_id            — Canonical user identifier. When set, it is applied
                       uniformly across every gateway (CLI, Telegram, Slack,
                       Discord, …) so the same human gets one merged memory
                       store. When unset, the gateway-native id (e.g. Telegram
                       numeric id, Discord snowflake) is used instead.
  agent_id           — Agent identifier (default: hermes)

The matching MEM0_MODE / MEM0_USER_ID / MEM0_AGENT_ID environment variables are
still read as a backward-compatible fallback, but mem0.json is the canonical
home for these non-secret settings.
"""

from __future__ import annotations

import atexit
import json
import logging
import threading
import time
from contextlib import suppress
from pathlib import Path
from typing import Any, Dict, List

from agent.memory_provider import MemoryProvider, spawn_context_thread
from agent.secret_scope import UnscopedSecretError, get_secret
from tools.registry import tool_error
from utils import atomic_json_write, read_json_or_empty

logger = logging.getLogger(__name__)

# Circuit breaker: after this many consecutive failures, pause API calls
# for _BREAKER_COOLDOWN_SECS to avoid hammering a down server.
_BREAKER_THRESHOLD = 5
_BREAKER_COOLDOWN_SECS = 120
_PREFETCH_WAIT_SECS = 3

_CLIENT_ERROR_TYPES = ("MemoryNotFoundError", "ValidationError")

# Sentinel returned when neither MEM0_USER_ID nor a gateway-native id is
# available. Treated as "no operator-configured user_id" by initialize() so
# that legacy mem0.json files written by the setup wizard (which historically
# wrote this exact placeholder) still allow gateway-native ids to flow
# through instead of silently overriding them with the placeholder.
_DEFAULT_USER_ID = "hermes-user"

# sync_turn sends the whole turn to the backend for fact extraction. OSS embedding
# models often have small context windows (bge-small-zh-v1.5: 512 tokens ≈ 500 chars;
# jina-embeddings-v3: 8192), and oversized turns make backend.add() raise — Ollama
# answers HTTP 500, hosted APIs return INPUT_TOKEN_LIMIT_EXCEEDED — which _try only
# logs, silently dropping the turn's memory extraction. Cap each message up front.
# The default fits a 512-token embedder (measured: 450 OK, 600 -> HTTP 500 on
# bge-small-zh-v1.5:f16); ``sync_max_chars`` in mem0.json raises it for larger windows.
_SYNC_MSG_MAX_CHARS = 450


# Sentence ends recognized when trimming a synced message. Deliberately unordered:
# the LAST boundary of ANY kind wins, so one CJK stop early in a mixed-script turn
# cannot outrank a Latin stop near the end of the window. ``".\n"`` is not listed —
# its index can never exceed the bare ``"."`` it starts with.
_SYNC_SENTENCE_ENDS = ("。", "！", "？", ".", "!", "?")


def _truncate_for_sync(text: str, max_len: int = _SYNC_MSG_MAX_CHARS) -> str:
    """Cap a synced message at its last sentence boundary within ``max_len``.

    Short messages pass through unchanged; long ones keep the last complete
    sentence inside the window so fact extraction still sees coherent statements,
    with a hard cut as fallback when no boundary exists (or one only appears in
    the first third of the window, which usually means unsegmented input).
    """
    if len(text) <= max_len:
        return text
    window = text[:max_len]
    cut = max(window.rfind(sep) for sep in _SYNC_SENTENCE_ENDS)
    if cut > max_len // 3:
        return text[:cut + 1]
    return text[:max_len]


def _is_client_error(exc: Exception) -> bool:
    """True for user-caused errors (bad ID, not found) that should NOT trip circuit breaker."""
    etype = type(exc).__name__
    if etype in _CLIENT_ERROR_TYPES:
        return True
    err_str = str(exc).lower()
    return "404" in err_str or "not found" in err_str or "valid uuid" in err_str


def _read_mem0_json(config_path: Path) -> dict:
    """Best-effort read of mem0.json; missing/corrupt file -> {}."""
    if config_path.exists():
        with suppress(Exception):
            return json.loads(config_path.read_text(encoding="utf-8"))
    return {}


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def _scoped_env(name: str) -> str:
    """Profile-scoped read of a non-secret mem0 setting; no scope under multiplex = unset (never
    ``os.environ``). Only the API key may fail closed — OSS mode has none to read (#99121)."""
    try:
        return get_secret(name, "") or ""
    except UnscopedSecretError:
        return ""


def _load_config() -> dict:
    """Load config from env vars, with $HERMES_HOME/mem0.json overrides.

    Environment variables provide defaults; mem0.json (if present) overrides
    individual keys.  This avoids a silent failure when the JSON file exists
    but is missing fields like ``api_key`` that the user set in ``.env``.
    """
    from hermes_constants import get_hermes_home
    # Identity (user/agent id), host and mode are .env values like the key: read them through the
    # profile scope too, or a secondary profile's memories land in the default profile's account.
    # A scope-less multiplex caller raises here on purpose — that is a spawn-site bug, and
    # swallowing it would silently route the turn's memories to the default profile.
    config = {"mode": get_secret("MEM0_MODE", "") or "platform", "host": get_secret("MEM0_HOST", "") or "",
              "agent_id": get_secret("MEM0_AGENT_ID", "") or "hermes", "oss": {}}
    if user_id := get_secret("MEM0_USER_ID", ""):  # only when explicitly configured, so initialize() can fall back to the gateway-native id
        config["user_id"] = user_id
    file_cfg = read_json_or_empty(get_hermes_home() / "mem0.json")
    config.update({k: v for k, v in file_cfg.items() if v is not None and v != ""})
    # MEM0_API_KEY authenticates the Platform and self-hosted HTTP backends; pure OSS mode builds its
    # backend from the local ``oss`` config and has no platform credential to resolve, so a profile
    # scope WITHOUT the key must still load an OSS config (#99121 as it stands today: the caller is
    # scoped, the scope is just empty). Decided after mem0.json overrode the env defaults because
    # the file may be what selects ``oss``. Scope-less callers already raised above.
    if config.get("mode", "platform") == "oss":
        config.setdefault("api_key", "")
    elif not config.get("api_key"):
        config["api_key"] = get_secret("MEM0_API_KEY", "")
    return config


# ---------------------------------------------------------------------------
# Tool schemas
# ---------------------------------------------------------------------------

SEARCH_SCHEMA = {
    "name": "mem0_search",
    "description": (
        "Search the user's memories by meaning; returns facts ranked by "
        "relevance. Use this before answering any question that may depend on "
        "what you know about the user (preferences, facts, history, people, "
        "projects, past decisions). For multi-part or multi-hop questions, "
        "call it several times — vary the wording and run follow-up searches "
        "on what earlier results reveal; one search is rarely enough."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "What to search for."},
            "top_k": {"type": "integer", "description": "Max results (default: 10, max: 50)."},
            "rerank": {"type": "boolean", "description": "Rerank results for relevance (default: false, platform mode only)."},
        },
        "required": ["query"],
    },
}

ADD_SCHEMA = {
    "name": "mem0_add",
    "description": (
        "Store a durable fact about the user, verbatim (no LLM extraction). "
        "Call this the moment the user states a lasting preference, correction, "
        "decision, or personal detail worth recalling on future turns — don't "
        "wait to be asked to remember. Skip transient chit-chat and facts you've "
        "already stored."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "content": {"type": "string", "description": "The fact to store."},
        },
        "required": ["content"],
    },
}

UPDATE_SCHEMA = {
    "name": "mem0_update",
    "description": (
        "Replace the text of an existing memory by its ID (take the ID from a "
        "mem0_search result). Use when a stored fact has changed "
        "or was wrong — correct it in place instead of adding a duplicate."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "memory_id": {"type": "string", "description": "Memory UUID to update."},
            "text": {"type": "string", "description": "New text content."},
        },
        "required": ["memory_id", "text"],
    },
}

DELETE_SCHEMA = {
    "name": "mem0_delete",
    "description": (
        "Delete a memory by its ID (take the ID from a mem0_search "
        "result). Use when a stored fact is obsolete or the user asks you to "
        "forget it; prefer mem0_update if the fact merely changed."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "memory_id": {"type": "string", "description": "Memory UUID to delete."},
        },
        "required": ["memory_id"],
    },
}

EXPORT_SCHEMA = {
    "name": "mem0_export",
    "description": (
        "Export all memories in the active user scope so a local/self-hosted "
        "mirror can audit or migrate them. Use this for verification, backup, "
        "or proof-of-local-path workflows; it returns the current scope in bulk."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "page": {"type": "integer", "description": "Page number (default: 1)."},
            "page_size": {"type": "integer", "description": "Rows per page (default: 100)."},
        },
    },
}


# ---------------------------------------------------------------------------
# MemoryProvider implementation
# ---------------------------------------------------------------------------

class Mem0MemoryProvider(MemoryProvider):
    """Mem0 memory with server-side extraction and semantic search.

    Supports Platform API (cloud) and OSS (self-hosted) modes via MEM0_MODE.
    """

    def __init__(self):
        self._config = None
        self._backend = None
        self._mode = "platform"
        self._api_key = ""
        self._host = ""
        self._user_id = _DEFAULT_USER_ID
        self._agent_id = "hermes"
        self._rerank_default = False
        self._channel = "cli"  # gateway channel name (cli/telegram/discord/...)
        self._session_id = ""
        self._parent_session_id = ""
        self._agent_context = "primary"
        self._agent_identity = ""
        self._agent_workspace = ""
        self._sync_max_chars = _SYNC_MSG_MAX_CHARS
        self._sync_thread = None
        self._prefetch_thread = None
        self._prefetch_query = ""
        self._prefetch_result = ""
        self._prefetch_done = False
        # Circuit breaker state
        self._consecutive_failures = 0
        self._breaker_open_until = 0.0
        self._breaker_lock = threading.Lock()
        self._sync_lock = threading.Lock()
        self._prefetch_lock = threading.Lock()
        self._atexit_registered = False

    @property
    def name(self) -> str:
        return "mem0"

    def is_available(self) -> bool:
        cfg = _load_config()
        mode = cfg.get("mode", "platform")
        if mode == "oss":
            return bool(cfg.get("oss", {}).get("vector_store"))
        # Platform needs an api_key; self-hosted needs a host (api_key optional
        # when the server runs with AUTH_DISABLED).
        return bool(cfg.get("api_key") or cfg.get("host"))

    def save_config(self, values, hermes_home):
        """Merge-write config to $HERMES_HOME/mem0.json."""
        config_path = Path(hermes_home) / "mem0.json"
        atomic_json_write(config_path, {**read_json_or_empty(config_path), **values}, mode=0o600)

    def get_config_schema(self):
        cfg = _load_config()
        mode = cfg.get("mode", "platform")
        api_key_required = mode != "oss"
        return [
            {"key": "api_key", "description": "Mem0 Platform API key", "secret": True, "required": api_key_required, "env_var": "MEM0_API_KEY", "url": "https://app.mem0.ai"},
            {"key": "host", "description": "Self-hosted Mem0 server URL (leave blank for cloud)", "required": False, "env_var": "MEM0_HOST"},
            {"key": "user_id", "description": "User identifier", "default": "hermes-user"},
            {"key": "agent_id", "description": "Agent identifier", "default": "hermes"},
            {"key": "rerank", "description": "Enable reranking for recall", "default": "false", "choices": ["true", "false"]},
        ]

    def post_setup(self, hermes_home: str, config: dict) -> None:
        from ._setup import post_setup
        post_setup(hermes_home, config)

    def _create_backend(self):
        # Lazy-install the mem0 SDK on demand before either backend imports
        # it. ensure() honors security.allow_lazy_installs (default true) and,
        # on a sealed Docker venv, redirects the install to the durable
        # target. On failure we fall through so the import inside the backend
        # produces the canonical error, captured below.
        try:
            from tools.lazy_deps import ensure as _lazy_ensure
            _lazy_ensure("memory.mem0", prompt=False)
        except ImportError:
            pass
        except Exception:
            pass
        try:
            if self._mode == "oss":
                from ._backend import OSSBackend
                return OSSBackend(self._config.get("oss", {}))
            if self._host:
                from ._backend import SelfHostedBackend
                return SelfHostedBackend(self._api_key, self._host)
            from ._backend import PlatformBackend
            return PlatformBackend(self._api_key)
        except Exception as e:
            logger.error("Mem0 backend failed to initialize (%s mode): %s", self._mode, e)
            self._init_error = str(e)
            return None

    def _is_breaker_open(self) -> bool:
        """Return True if the circuit breaker is tripped (too many failures)."""
        with self._breaker_lock:
            if self._consecutive_failures < _BREAKER_THRESHOLD:
                return False
            if time.monotonic() >= self._breaker_open_until:
                self._consecutive_failures = 0
                return False
            return True

    def _format_error(self, prefix: str, exc: Exception) -> str:
        msg = f"{prefix}: {exc}"
        if self._mode == "oss":
            err_str = str(exc).lower()
            if "connection" in err_str or "refused" in err_str or "timeout" in err_str:
                vs = self._config.get("oss", {}).get("vector_store", {})
                msg += f" (check that {vs.get('provider', 'vector store')} is running)"
        return msg

    def _record_success(self):
        with self._breaker_lock:
            self._consecutive_failures = 0

    def _record_failure(self):
        with self._breaker_lock:
            self._consecutive_failures += 1
            count = self._consecutive_failures
            if count >= _BREAKER_THRESHOLD:
                self._breaker_open_until = time.monotonic() + _BREAKER_COOLDOWN_SECS
            else:
                count = 0
        if count >= _BREAKER_THRESHOLD:
            hint = ""
            if self._mode == "oss":
                vs = self._config.get("oss", {}).get("vector_store", {})
                provider = vs.get("provider", "unknown")
                hint = f" Check that your {provider} vector store is running and reachable."
            logger.warning(
                "Mem0 circuit breaker tripped after %d consecutive failures. "
                "Pausing API calls for %ds.%s",
                count, _BREAKER_COOLDOWN_SECS, hint,
            )

    def initialize(self, session_id: str, **kwargs) -> None:
        cfg = self._config = _load_config()
        self._mode = cfg.get("mode", "platform")
        self._api_key = cfg.get("api_key", "")
        self._host = cfg.get("host", "")
        self._session_id = session_id
        self._parent_session_id = str(kwargs.get("parent_session_id") or "")
        self._agent_context = str(kwargs.get("agent_context") or "primary")
        self._agent_identity = str(kwargs.get("agent_identity") or "")
        self._agent_workspace = str(kwargs.get("agent_workspace") or "")
        # Resolution order for user_id:
        #   1. Operator-configured MEM0_USER_ID (env or $HERMES_HOME/mem0.json) —
        #      the canonical principal, applied across every gateway so the same
        #      human gets one merged memory store.
        #   2. Gateway-native id from kwargs (Telegram numeric id, Discord
        #      snowflake, etc.) — preserves per-platform isolation when no
        #      override is configured.
        #   3. Hardcoded fallback _DEFAULT_USER_ID (CLI with no auth).
        # The literal _DEFAULT_USER_ID string is treated as unset so users who
        # ran the setup wizard with the suggested default still get gateway-
        # native ids instead of being silently bucketed together.
        configured = self._config.get("user_id")
        if configured == _DEFAULT_USER_ID:
            configured = None
        self._user_id = configured or kwargs.get("user_id") or _DEFAULT_USER_ID
        self._agent_id = cfg.get("agent_id", "hermes")
        # Persisted rerank preference (setup wizard / mem0.json). Used as the
        # DEFAULT for mem0_search when the model doesn't pass ``rerank``
        # explicitly; per-call args still win. Platform-only feature — other
        # backends accept-and-ignore the flag.
        _rr = cfg.get("rerank", False)
        self._rerank_default = (
            _rr.lower() in ("true", "1", "yes") if isinstance(_rr, str) else bool(_rr)
        )
        self._channel = kwargs.get("platform") or "cli"
        self._sync_max_chars = int(cfg.get("sync_max_chars") or _SYNC_MSG_MAX_CHARS)
        self._backend = self._create_backend()
        if self._backend and not self._atexit_registered:
            atexit.register(self._shutdown_backend)
            self._atexit_registered = True

    def _read_filters(self) -> Dict[str, Any]:
        # Scoped to user_id only — by design — so recall surfaces memories
        # written from any gateway/agent under this principal. Writes attach
        # agent_id (and metadata.channel) so per-agent / per-channel views are
        # still possible at query time when needed; reads default to the wider
        # cross-agent recall.
        return {"user_id": self._user_id}

    def _write_metadata(self) -> Dict[str, Any]:
        # Tag every write with scope, provenance, retention, and consent so
        # self-hosted mirrors can replay and audit the write without the live
        # agent process.
        return {
            "channel": self._channel,
            "scope": {
                "target": "memory",
                "user_id": self._user_id,
                "agent_id": self._agent_id,
                "channel": self._channel,
                "identity": self._agent_identity,
                "workspace": self._agent_workspace,
            },
            "provenance": {
                "session_id": self._session_id,
                "parent_session_id": self._parent_session_id,
                "platform": self._channel,
                "agent_context": self._agent_context,
            },
            "retention": {"policy": "provider-default", "scope": "profile"},
            "consent": {"granted": True, "mode": "implicit", "source": "direct_tool"},
        }

    def search(self, query: str, *, top_k: int = 10, rerank: bool | None = None) -> list[dict]:
        if self._backend is None:
            raise RuntimeError("Mem0 backend not initialized")
        return self._backend.search(
            query,
            filters=self._read_filters(),
            top_k=top_k,
            rerank=self._rerank_default if rerank is None else bool(rerank),
        )

    def add(
        self,
        messages: list,
        *,
        infer: bool = False,
        metadata: dict | None = None,
    ) -> dict:
        if self._backend is None:
            raise RuntimeError("Mem0 backend not initialized")

        def _merge(base: dict, extra: dict) -> dict:
            merged = dict(base)
            for key, value in extra.items():
                if isinstance(merged.get(key), dict) and isinstance(value, dict):
                    merged[key] = _merge(dict(merged[key]), value)
                else:
                    merged[key] = value
            return merged

        merged_metadata = self._write_metadata()
        if metadata:
            merged_metadata = _merge(merged_metadata, metadata)
        return self._backend.add(
            messages,
            user_id=self._user_id,
            agent_id=self._agent_id,
            infer=infer,
            metadata=merged_metadata,
        )

    def delete(self, memory_id: str) -> dict:
        if self._backend is None:
            raise RuntimeError("Mem0 backend not initialized")
        return self._backend.delete(memory_id)

    def export(self, *, page: int = 1, page_size: int = 100) -> dict:
        if self._backend is None:
            raise RuntimeError("Mem0 backend not initialized")
        return self._backend.export(filters=self._read_filters(), page=page, page_size=page_size)

    def system_prompt_block(self) -> str:
        # Mirror the precedence in _create_backend (oss > host > platform) so
        # the label always names the backend that actually runs. Checking
        # ``host`` first here would mislabel an ``oss``+``host`` config as
        # self-hosted HTTP even though OSS wins the routing.
        if self._mode == "oss":
            mode_label = "OSS (self-hosted)"
        elif self._host:
            mode_label = "self-hosted (HTTP API)"
        else:
            mode_label = "platform (cloud API)"
        # Rerank is a Mem0 Platform feature only.
        rerank_note = " Rerank is available on search." if (self._mode == "platform" and not self._host) else ""
        return (
            "# Mem0 Memory\n"
            f"Active. Mode: {mode_label}. User: {self._user_id}.\n"
            "You have persistent memory of this user from past conversations. "
            "You should call mem0_search before answering anything that could depend "
            "on prior context (the user's preferences, facts, history, people, "
            "projects, or earlier decisions) — do not rely on the chat window "
            "alone, and do not assume you have no memory.\n"
            "For multi-part or multi-hop questions, run several searches with "
            "different wording/angles and follow-up searches on what the first "
            "results surface; one search is rarely enough. Keep searching until "
            "you have every fact the question needs before you answer.\n"
            "Tools: mem0_search to find memories, mem0_add to store facts, "
            f"mem0_update, mem0_delete, and mem0_export to manage by ID or "
            f"dump the current scope.{rerank_note}"
        )

    def on_turn_start(self, turn_number: int, message: str, **kwargs) -> None:
        self._start_prefetch(message)

    def _consume_prefetch_result(self, query: str) -> str | None:
        with self._prefetch_lock:
            if self._prefetch_query != query or not self._prefetch_done:
                return None
            result = self._prefetch_result
            self._prefetch_result = ""
            self._prefetch_done = False
            return result

    def _start_prefetch(self, query: str) -> None:
        if not query or self._backend is None or self._is_breaker_open():
            return
        backend = self._backend
        with self._prefetch_lock:
            if self._prefetch_query == query:
                if self._prefetch_done:
                    return
                if self._prefetch_thread and self._prefetch_thread.is_alive():
                    return
            self._prefetch_query = query
            self._prefetch_result = ""
            self._prefetch_done = False

        def _run():
            body = ""
            try:
                results = backend.search(
                    query, filters=self._read_filters(), top_k=10, rerank=False,
                )
                lines = [r.get("memory", "") for r in (results or []) if r.get("memory")]
                if lines:
                    body = "## Mem0 Memory\n" + "\n".join(f"- {l}" for l in lines)
                self._record_success()
            except Exception as e:
                self._record_failure()
                logger.debug("Mem0 prefetch failed: %s", e)
            with self._prefetch_lock:
                if self._prefetch_query == query:
                    self._prefetch_result = body
                    self._prefetch_done = True

        with self._prefetch_lock:
            # Same query already answered or still in flight: don't restart it.
            if self._prefetch_query == query and (self._prefetch_done or (self._prefetch_thread and self._prefetch_thread.is_alive())):
                return
            self._prefetch_query, self._prefetch_result, self._prefetch_done = query, "", False
            self._prefetch_thread = t = spawn_context_thread(_run, name="mem0-prefetch")
        t.start()

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        """Recall memories for the CURRENT question with a short hot-path wait."""
        cached = self._consume_prefetch_result(query)
        if cached is not None:
            return cached
        self._start_prefetch(query)
        with self._prefetch_lock:
            thread = self._prefetch_thread if self._prefetch_query == query else None
        if thread:
            thread.join(timeout=_PREFETCH_WAIT_SECS)
        cached = self._consume_prefetch_result(query)
        if cached is not None:
            return cached
        # Slow backend: skip injection; mem0_search tool remains the backstop.
        return ""

    def sync_turn(self, user_content: str, assistant_content: str, *, session_id: str = "") -> None:
        """Send the turn to Mem0 for server-side fact extraction (non-blocking)."""
        if self._backend is None or self._is_breaker_open():
            return

        def _sync():
            backend = self._backend
            if backend is None:
                return
            try:
                messages = [
                    {"role": "user", "content": _truncate_for_sync(user_content, self._sync_max_chars)},
                    {"role": "assistant", "content": _truncate_for_sync(assistant_content, self._sync_max_chars)},
                ]
                backend.add(
                    messages,
                    user_id=self._user_id,
                    agent_id=self._agent_id,
                    infer=True,
                    metadata=self._write_metadata(),
                )
                self._record_success()
            except Exception as e:
                self._record_failure()
                logger.warning("Mem0 sync failed: %s", e)

        with self._sync_lock:
            if self._sync_thread and self._sync_thread.is_alive():
                self._sync_thread.join(timeout=5.0)
            # If still alive after timeout, skip to avoid duplicate ingestion.
            if self._sync_thread and self._sync_thread.is_alive():
                return
            self._sync_thread = spawn_context_thread(_sync, name="mem0-sync")
            self._sync_thread.start()

    def get_tool_schemas(self) -> List[Dict[str, Any]]:
        return [SEARCH_SCHEMA, ADD_SCHEMA, UPDATE_SCHEMA, DELETE_SCHEMA, EXPORT_SCHEMA]

    def handle_tool_call(self, tool_name: str, args: dict, **kwargs) -> str:
        if self._backend is None:
            err = getattr(self, "_init_error", "unknown error")
            hint = ""
            if self._mode == "oss":
                vs = self._config.get("oss", {}).get("vector_store", {})
                provider = vs.get("provider", "vector store")
                hint = f" Check that {provider} is running and reachable."
            return json.dumps({"error": f"Mem0 backend not initialized: {err}.{hint}"})

        if self._is_breaker_open():
            msg = "Mem0 temporarily unavailable (multiple consecutive failures). Will retry automatically."
            if self._mode == "oss":
                vs = self._config.get("oss", {}).get("vector_store", {})
                msg += f" Check that your {vs.get('provider', 'vector store')} is running."
            return json.dumps({"error": msg})

        if tool_name == "mem0_search":
            query = args.get("query", "")
            if not query:
                return tool_error("Missing required parameter: query")
            try:
                top_k = max(1, min(int(args.get("top_k", 10)), 50))
                rerank_raw = args.get("rerank", getattr(self, "_rerank_default", False))
                if isinstance(rerank_raw, str):
                    rerank = rerank_raw.lower() not in ("false", "0", "no")
                else:
                    rerank = bool(rerank_raw)
                results = self._backend.search(query, filters=self._read_filters(), top_k=top_k, rerank=rerank)
                self._record_success()
                if not results:
                    return json.dumps({"result": "No relevant memories found."})
                items = [{"id": r.get("id"), "memory": r.get("memory", ""),
                          "score": r.get("score", 0)} for r in results]
                return json.dumps({"results": items, "count": len(items)})
            except Exception as e:
                if not _is_client_error(e):
                    self._record_failure()
                return tool_error(self._format_error("Search failed", e))

        elif tool_name == "mem0_add":
            content = args.get("content", "")
            if not content:
                return tool_error("Missing required parameter: content")
            try:
                result = self._backend.add(
                    [{"role": "user", "content": content}],
                    user_id=self._user_id,
                    agent_id=self._agent_id,
                    infer=False,
                    metadata=self._write_metadata(),
                )
                self._record_success()
                event_id = result.get("event_id") if isinstance(result, dict) else None
                # Cloud add is async (server-side extraction); OSS and self-hosted store synchronously.
                msg = "Fact stored." if (self._mode == "oss" or self._host) else "Fact queued for storage."
                return json.dumps({"result": msg, "event_id": event_id})
            except Exception as e:
                self._record_failure()
                return tool_error(self._format_error("Failed to store", e))

        elif tool_name == "mem0_update":
            memory_id = args.get("memory_id", "")
            text = args.get("text", "")
            if not memory_id:
                return tool_error("Missing required parameter: memory_id")
            if not text:
                return tool_error("Missing required parameter: text")
            try:
                result = self._backend.update(memory_id, text)
                self._record_success()
                return json.dumps(result)
            except Exception as e:
                if _is_client_error(e):
                    return tool_error(f"Memory not found: {memory_id}")
                self._record_failure()
                return tool_error(self._format_error("Update failed", e))

        elif tool_name == "mem0_delete":
            memory_id = args.get("memory_id", "")
            if not memory_id:
                return tool_error("Missing required parameter: memory_id")
            try:
                result = self._backend.delete(memory_id)
                self._record_success()
                return json.dumps(result)
            except Exception as e:
                if _is_client_error(e):
                    return tool_error(f"Memory not found: {memory_id}")
                self._record_failure()
                return tool_error(self._format_error("Delete failed", e))

        elif tool_name == "mem0_export":
            try:
                page = max(1, int(args.get("page", 1) or 1))
            except Exception:
                page = 1
            try:
                page_size = max(1, min(int(args.get("page_size", 100) or 100), 500))
            except Exception:
                page_size = 100
            try:
                result = self._backend.export(
                    filters=self._read_filters(), page=page, page_size=page_size
                )
                self._record_success()
                if isinstance(result, dict):
                    return json.dumps(result)
                return json.dumps({"results": result, "count": len(result) if isinstance(result, list) else 0})
            except Exception as e:
                self._record_failure()
                return tool_error(self._format_error("Export failed", e))

        return tool_error(f"Unknown tool: {tool_name}")

    def _shutdown_backend(self):
        try:
            if self._backend:
                self._backend.close()
                self._backend = None
        except Exception:
            pass

    def shutdown(self) -> None:
        for t in (self._prefetch_thread, self._sync_thread):
            if t and t.is_alive():
                t.join(timeout=5.0)
        self._shutdown_backend()


def register(ctx) -> None:
    """Register Mem0 as a memory provider plugin."""
    ctx.register_memory_provider(Mem0MemoryProvider())
