"""Progressive subdirectory hint discovery.

As the agent navigates into subdirectories via tool calls (read_file, terminal,
search_files, etc.), this module discovers and loads project context files
(AGENTS.md, CLAUDE.md, .cursorrules) from those directories.  Discovered hints
are appended to the tool result so the model gets relevant context at the moment
it starts working in a new area of the codebase.

This complements the startup context loading in ``prompt_builder.py`` which only
loads from the CWD.  Subdirectory hints are discovered lazily and injected into
the conversation without modifying the system prompt (preserving prompt caching).

Inspired by Block/goose's SubdirectoryHintTracker.
"""

import hashlib
import logging
import os
import shlex
from pathlib import Path
from typing import Dict, Any, Optional, Set

from hermes_constants import get_real_home
from agent.prompt_builder import _read_text_with_timeout, _scan_context_content, _truncate_content
from agent.search_policy import SEARCH_PRUNE_DIR_NAMES

logger = logging.getLogger(__name__)

# Context files to look for in subdirectories, in priority order.
# Same filenames as prompt_builder.py but we load ALL found (not first-wins)
# since different subdirectories may use different conventions.
_HINT_FILENAMES = [
    "AGENTS.override.md",
    "AGENTS.md", "agents.md",
    "CLAUDE.md", "claude.md",
    ".cursorrules",
]

# Maximum chars per hint file to prevent context bloat. Keep area-level
# instructions intact while allowing the shared helper to retain both head
# and tail when a file exceeds the ceiling.
_MAX_HINT_CHARS = 32_000

# Tool argument keys that typically contain file paths
_PATH_ARG_KEYS = {"path", "file_path", "workdir"}

# Tools that take shell commands where we should extract paths
_COMMAND_TOOLS = {"terminal"}

# How many parent directories to walk up when looking for hints.
# Prevents scanning all the way to / for deeply nested paths.
_MAX_ANCESTOR_WALK = 5

# Shared with broad recursive search probes so context discovery and search do
# not drift into different dependency/cache/build trees.
_EXCLUDED_DIR_NAMES = SEARCH_PRUNE_DIR_NAMES


def _is_ancestor_or_same(a: Path, b: Path) -> bool:
    """Check if *a* is the same as or an ancestor of *b* (parent directory check)."""
    try:
        b.relative_to(a)
        return True
    except ValueError:
        return False


def _resolve_candidate_path(raw_path: str, working_dir: Path) -> Path:
    """Resolve a path-like tool argument without inheriting profile HOME.

    ``Path.expanduser()`` follows the current process HOME, which can be a
    profile-scoped Hermes home in some runtimes. For tool arguments we want the
    user's real home so ``~/...`` resolves the same way regardless of which
    profile is active.
    """
    raw_path = raw_path.strip()
    p = Path(raw_path)
    if raw_path == "~" or raw_path.startswith("~/"):
        p = Path(get_real_home()) / raw_path[2:]
    elif raw_path.startswith("~"):
        p = Path(raw_path).expanduser()
    if not p.is_absolute():
        p = working_dir / p
    return p


_NAV_COMMANDS = frozenset({"cd", "pushd"})
_SHELL_OPERATORS = frozenset({"&&", "||", "|", ";", "&", ";;", "|&", "(", ")"})


def _nav_targets(cmd: str) -> list:
    """Operands of `cd` / `pushd` that begin a shell segment. `cd -` and bare `cd` yield nothing."""
    lexer = shlex.shlex(cmd, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    try:
        tokens = list(lexer)
    except ValueError:
        return []
    targets, segment_start = [], True
    for idx, token in enumerate(tokens):
        if token in _SHELL_OPERATORS:
            segment_start = True
            continue
        if segment_start and token in _NAV_COMMANDS:
            operand = next((t for t in tokens[idx + 1:] if t in _SHELL_OPERATORS or not t.startswith("-")), None)
            if operand and operand not in _SHELL_OPERATORS:
                targets.append(operand)
        segment_start = False
    return targets


class SubdirectoryHintTracker:
    """Track which directories the agent visits and load hints on first access.

    Usage::

        tracker = SubdirectoryHintTracker(working_dir="/path/to/project")

        # After each tool call:
        hints = tracker.check_tool_call("read_file", {"path": "backend/src/main.py"})
        if hints:
            tool_result += hints  # append to the tool result string
    """

    def __init__(self, working_dir: Optional[str] = None, *, enabled: bool = True):
        # ``enabled=False`` mirrors ``skip_context_files``: a session that opted out of
        # AGENTS.md/CLAUDE.md injection at startup must not get the same files spliced into
        # tool results later — cron jobs relaying exact stdout leaked them to chat (#9441).
        self.enabled = enabled
        self.working_dir = Path(working_dir or os.getcwd()).resolve()
        self._loaded_dirs: Set[Path] = set()
        # Content digests already injected — prevents re-sending the same file
        # reachable through symlinks, hardlinks, or duplicated copies.
        self._loaded_digests: Set[str] = set()
        # Pre-mark the working dir as loaded (startup context handles it)
        self._loaded_dirs.add(self.working_dir)
        self._seed_working_dir_digest()

    def _seed_working_dir_digest(self) -> None:
        """Record the CWD context file's digest so it is never re-injected.

        ``prompt_builder`` already loads the working directory's context file at
        startup.  Seeding its digest here means the same content reached through
        a different path (a symlink farm, a shared workspace) is recognised as a
        duplicate instead of being sent a second time.
        """
        for filename in _HINT_FILENAMES:
            candidate = self.working_dir / filename
            try:
                if not candidate.is_file():
                    continue
                content = (_read_text_with_timeout(candidate) or "").strip()
            except (OSError, UnicodeDecodeError):
                continue
            if content:
                self._loaded_digests.add(
                    hashlib.sha256(content.encode("utf-8")).hexdigest()
                )
            break  # first match wins, mirroring startup loading

    def check_tool_call(
        self,
        tool_name: str,
        tool_args: Dict[str, Any],
    ) -> Optional[str]:
        """Check tool call arguments for new directories and load any hint files.

        Returns formatted hint text for newly visited directories, or None.
        """
        if not self.enabled:
            return None
        dirs = self._extract_directories(tool_name, tool_args)
        if not dirs:
            return None

        all_hints = []
        for d in dirs:
            hints = self._load_hints_for_directory(d)
            if hints:
                all_hints.append(hints)

        if not all_hints:
            return None

        return "\n\n" + "\n\n".join(all_hints)

    def _extract_directories(
        self, tool_name: str, args: Dict[str, Any]
    ) -> list:
        """Extract directory paths from tool call arguments."""
        candidates: Set[Path] = set()

        # Direct path arguments
        for key in _PATH_ARG_KEYS:
            val = args.get(key)
            if isinstance(val, str) and val.strip():
                self._add_path_candidate(val, candidates)

        # Shell commands — extract path-like tokens
        if tool_name in _COMMAND_TOOLS:
            cmd = args.get("command", "")
            if isinstance(cmd, str):
                self._extract_paths_from_command(cmd, candidates)

        return list(candidates)

    def _add_path_candidate(self, raw_path: str, candidates: Set[Path]):
        """Resolve a raw path and add its directory + ancestors to candidates.

        Walks up from the resolved directory toward the filesystem root,
        stopping at the first directory already in ``_loaded_dirs`` (or after
        ``_MAX_ANCESTOR_WALK`` levels).  This ensures that reading
        ``project/src/main.py`` discovers ``project/AGENTS.md`` even when
        ``project/src/`` has no hint files of its own.
        """
        try:
            p = _resolve_candidate_path(raw_path, self.working_dir).resolve()
            # Use parent if it's a file path (has extension or doesn't exist as dir)
            if p.suffix or (p.exists() and p.is_file()):
                p = p.parent
            # Walk up ancestors — stop at already-loaded or root
            for _ in range(_MAX_ANCESTOR_WALK):
                if p in self._loaded_dirs:
                    break
                if self._is_valid_subdir(p):
                    candidates.add(p)
                parent = p.parent
                if parent == p:
                    break  # filesystem root
                p = parent
        except (OSError, ValueError, RuntimeError):
            pass

    def _extract_paths_from_command(self, cmd: str, candidates: Set[Path]):
        """Extract path-like tokens from a shell command string."""
        try:
            tokens = shlex.split(cmd)
        except ValueError:
            tokens = cmd.split()
        # `cd backend && ls`: a bare directory name has no `/` or `.`, so the generic filter below drops
        # it; the operand of a navigation command is a path by construction (#11032). Only a `cd` at the
        # START of a shell segment counts (`echo cd backend` is prose); punctuation-aware tokenizing keeps
        # a quoted `'backend;'` literal while splitting bare `backend;ls` at the operator.
        for target in _nav_targets(cmd):
            self._add_path_candidate(target, candidates)
        for token in tokens:
            # Skip flags
            if token.startswith("-"):
                continue
            # Must look like a path (contains / or .)
            if "/" not in token and "." not in token:
                continue
            # Skip URLs
            if token.startswith(("http://", "https://", "git@")):
                continue
            self._add_path_candidate(token, candidates)

    def _is_valid_subdir(self, path: Path) -> bool:
        """Check if path is a valid directory to scan for hints.

        Only allow subdirectories within the working directory tree.
        This prevents loading AGENTS.md from outside the active workspace
        (e.g. a ``~``-expanded path that lands in a different profile home),
        which causes cross-agent context contamination and instruction mixup.
        """
        try:
            if not path.is_dir():
                return False
        except OSError:
            return False
        if path in self._loaded_dirs:
            return False
        # Reject paths outside the working directory tree.
        # path.resolve() may differ from working_dir.resolve() due to symlinks,
        # but path.is_relative_to(working_dir) handles both absolute and
        # symlinked paths correctly on Python 3.9+.
        try:
            if not path.is_relative_to(self.working_dir):
                return False
        except (OSError, ValueError):
            # Older Python or path resolution error — fall back to parent
            # check as a best-effort safeguard.
            if not _is_ancestor_or_same(self.working_dir, path):
                return False
        if self._is_excluded(path):
            return False
        return True

    def _is_excluded(self, path: Path) -> bool:
        """True when the path sits inside a directory that holds copies, not context.

        Directories the user is deliberately working inside are never excluded —
        if ``working_dir`` is itself under ``vendor/``, that segment is legitimate
        and only segments *below* the working dir are screened.
        """
        try:
            rel_parts = path.relative_to(self.working_dir).parts
        except ValueError:
            # Paths outside the working dir are already rejected by
            # _is_valid_subdir before this runs; treat as excluded defensively.
            return True
        return any(part in _EXCLUDED_DIR_NAMES for part in rel_parts)

    def _load_hints_for_directory(self, directory: Path) -> Optional[str]:
        """Load hint files from a directory. Returns formatted text or None.

        Only loads hints from directories within the working directory tree.
        """
        self._loaded_dirs.add(directory)

        # Reject paths outside the working directory tree.
        try:
            if not directory.is_relative_to(self.working_dir):
                logger.debug(
                    "Skipping hint files in %s — outside working_dir %s",
                    directory, self.working_dir,
                )
                return None
        except (OSError, ValueError):
            if not _is_ancestor_or_same(self.working_dir, directory):
                logger.debug(
                    "Skipping hint files in %s — outside working_dir %s",
                    directory, self.working_dir,
                )
                return None

        found_hints = []
        for filename in _HINT_FILENAMES:
            hint_path = directory / filename
            try:
                if not hint_path.is_file():
                    continue
            except OSError:
                continue
            try:
                content = (_read_text_with_timeout(hint_path) or "").strip()
                if not content:
                    continue
                # Skip content we've already injected. The same AGENTS.md is
                # routinely reachable through several paths (symlinked shared
                # workspaces, hardlinks, copied backups); re-sending it burns
                # context for zero new information.
                digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
                if digest in self._loaded_digests:
                    logger.debug(
                        "Skipping duplicate hint content at %s (digest %s)",
                        hint_path,
                        digest[:12],
                    )
                    break
                self._loaded_digests.add(digest)
                # Same security scan as startup context loading
                content = _scan_context_content(content, filename)
                # Best-effort relative path for display
                rel_path = str(hint_path)
                try:
                    rel_path = str(hint_path.relative_to(self.working_dir))
                except (ValueError, RuntimeError):
                    try:
                        # as_posix: "~/" shorthand implies POSIX rendering
                        # (avoids ~/AppData\Local\... chimeras on Windows).
                        rel_path = "~/" + hint_path.relative_to(Path.home()).as_posix()
                    except (ValueError, RuntimeError):
                        pass  # keep absolute
                content = _truncate_content(
                    content,
                    filename,
                    max_chars=_MAX_HINT_CHARS,
                    read_path=rel_path,
                    queue_warning=False,
                )
                found_hints.append((rel_path, content))
                # First match wins per directory (like startup loading)
                break
            except Exception as exc:
                logger.debug("Could not read %s: %s", hint_path, exc)

        if not found_hints:
            return None

        sections = []
        for rel_path, content in found_hints:
            sections.append(
                f"[Subdirectory context discovered: {rel_path}]\n{content}"
            )

        logger.debug(
            "Loaded subdirectory hints from %s: %s",
            directory,
            [h[0] for h in found_hints],
        )
        return "\n\n".join(sections)
