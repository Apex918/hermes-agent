#!/usr/bin/env python3
"""Shared handlers for the /memory and /skills write-approval subcommands.

Both the interactive CLI (``cli.py``) and the gateway (``gateway/run.py``) call
into this module so the pending-review UX (list / approve / reject / diff /
mode) lives in one place. Each caller owns only its surface concerns:
formatting the returned text and, for the gateway, persisting config + evicting
the cached agent on a mode change.

Every public handler returns a plain text string suitable for both a terminal
and a chat message. Skill diffs are intentionally NOT inlined here — the
``diff`` handler returns the full diff for the CLI pager, but on a messaging
platform the gateway truncates it and points the user at the dashboard / file.
"""

from __future__ import annotations

import json
from typing import List, Optional

from tools import write_approval as wa


def _fmt_state(subsystem: str) -> str:
    on = wa.write_approval_enabled(subsystem)
    return f"{subsystem}.write_approval = {'on' if on else 'off'}"


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------

def _fmt_pending_list(subsystem: str) -> str:
    records = wa.list_pending(subsystem)
    if not records:
        return f"No pending {subsystem} writes."
    lines = [f"Pending {subsystem} writes ({len(records)}):"]
    for r in records:
        origin = r.get("origin", "foreground")
        tag = " [auto]" if origin == "background_review" else ""
        lines.append(f"  {r['id']}{tag}  {r.get('summary', '')}")
        if subsystem == wa.MEMORY:
            lines.extend(f"      {line}" for line in _matched_entries(r["payload"]))
    where = "/{s} approve <id>".format(s=subsystem)
    lines.append("")
    lines.append(f"Apply: {where}   Reject: /{subsystem} reject <id>")
    if subsystem == wa.SKILLS:
        lines.append("Review full diff: /skills diff <id>")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Subcommand dispatch
# ---------------------------------------------------------------------------

def handle_pending_subcommand(
    subsystem: str,
    args: List[str],
    *,
    memory_store=None,
    set_mode_fn=None,
) -> Optional[str]:
    """Dispatch a /memory or /skills subcommand.

    Args:
        subsystem: ``memory`` or ``skills``.
        args: tokens after the slash command (e.g. ``["approve", "a1b2"]``).
        memory_store: live MemoryStore for applying approved memory writes
            (CLI passes ``self.agent._memory_store``; gateway applies against a
            freshly loaded store).
        set_mode_fn: optional callable ``(enabled: bool) -> None`` that
            persists the new write_approval boolean to config (gateway provides
            this; CLI uses its own ``save_config_value`` and passes a closure).

    Returns a text string to show the user. Returns None when the args are not
    a write-approval subcommand (caller falls through to its other handling,
    e.g. /skills search).
    """
    if not args:
        # Bare /memory or /skills with no sub → show pending + gate state.
        return f"{_fmt_state(subsystem)}\n\n" + _fmt_pending_list(subsystem)

    sub = args[0].lower()
    rest = args[1:]

    if sub == "pending":
        return _fmt_pending_list(subsystem)

    if sub in {"approve", "apply"}:
        return _approve(subsystem, rest, memory_store)

    if sub in {"reject", "deny", "drop"}:
        return _reject(subsystem, rest)

    if sub == "diff" and subsystem == wa.SKILLS:
        return _diff(rest)

    if sub in {"approval", "mode"}:  # 'mode' kept as a back-compat alias
        return _set_approval(subsystem, rest, set_mode_fn)

    return None  # not ours — caller handles


def _resolve_one(subsystem: str, rest: List[str]):
    if not rest:
        return None, f"Usage: /{subsystem} approve|reject <id>  (or 'all')"
    return rest[0], None


def _approve(subsystem: str, rest: List[str], memory_store) -> str:
    target, err = _resolve_one(subsystem, rest)
    if err or target is None:
        return err or f"Usage: /{subsystem} approve <id>"

    receipt = rest[1] if len(rest) > 1 else None

    records = wa.list_pending(subsystem)
    if not records:
        return f"No pending {subsystem} writes."

    if target.lower() == "all":
        targets = list(records)
    else:
        rec = wa.get_pending(subsystem, target)
        if not rec:
            return f"No pending {subsystem} write with id '{target}'."
        targets = [rec]

    applied, failed, overwritten, removed = 0, [], [], []
    for rec in targets:
        ok, msg, result = _apply_one(subsystem, rec, memory_store, receipt=receipt)
        if ok:
            wa.discard_pending(subsystem, rec["id"])
            applied += 1
            overwritten.extend(f"  {rec['id']}: {text}" for text in _changed_entries(result, "replaced"))
            removed.extend(f"  {rec['id']}: {text}" for text in _changed_entries(result, "removed"))
        else:
            failed.append(f"{rec['id']}: {msg}")

    out = [f"Approved {applied} {subsystem} write(s)."]
    if overwritten:
        # A memory 'replace' overwrites the WHOLE matched entry (#117952); the approver
        # is the last person who can notice a clause went missing, so show what was lost.
        out.append("Overwrote entire entry (re-add anything you still need):")
        out.extend(overwritten)
    if removed:
        out.append("Removed entry (re-add anything you still need):")
        out.extend(removed)
    if failed:
        out.append("Failed:")
        out.extend(f"  {f}" for f in failed)
    return "\n".join(out)


def _changed_entries(result: dict, kind: str) -> List[str]:
    """Full text of every entry a memory replace overwrote (``kind="replaced"``) or remove
    deleted (``"removed"``), single-op or batch shape."""
    single = result.get(f"{kind}_entry")
    batch = result.get(f"{kind}_entries") or {}
    return ([single] if single else []) + [batch[k] for k in sorted(batch, key=int)]


def _matched_entries(payload) -> List[str]:
    """The full entry each staged memory replace/remove is pinned to: the summary shows only
    the old_text search string, and approval applies to this entry, not to that search."""
    from tools.memory_tool import destructive_ops
    return [f"{op['action']}s entry: {op['matched_entry']}" if op.get("matched_entry")
            else f"{op['action']}: unpinned legacy target \u2014 reject and recreate before approving"
            for op in destructive_ops(payload)]


def _apply_one(subsystem: str, rec, memory_store, *, receipt=None):
    """``(ok, error, result)`` — *result* is the applier's full payload (empty on exceptions)."""
    payload = rec.get("payload", {})
    try:
        effect = None
        if subsystem == wa.MEMORY:
            def _memory_effect():
                if memory_store is None:
                    return {"success": False, "error": "memory store unavailable"}
                from tools.memory_tool import apply_memory_pending
                return apply_memory_pending(payload, memory_store)
            effect = _memory_effect
        else:
            def _skill_effect():
                from tools.skill_manager_tool import apply_skill_pending
                return json.loads(apply_skill_pending(payload))
            effect = _skill_effect

        approval = wa.apply_pending(rec, effect, receipt=receipt)
        if not approval.get("success"):
            return False, approval.get("error", ""), {}
        result = approval.get("result", {})
        if isinstance(result, dict):
            return bool(result.get("success", True)), result.get("error", ""), result
        return True, "", {}
    except Exception as e:
        return False, str(e), {}


def _reject(subsystem: str, rest: List[str]) -> str:
    target, err = _resolve_one(subsystem, rest)
    if err or target is None:
        return err or f"Usage: /{subsystem} reject <id>"
    if target.lower() == "all":
        n = 0
        for rec in wa.list_pending(subsystem):
            if wa.reject_pending(subsystem, rec["id"]):
                n += 1
        return f"Rejected {n} pending {subsystem} write(s)."
    if wa.reject_pending(subsystem, target):
        return f"Rejected pending {subsystem} write '{target}'."
    return f"No pending {subsystem} write with id '{target}'."


def _diff(rest: List[str]) -> str:
    if not rest:
        return "Usage: /skills diff <id>"
    rec = wa.get_pending(wa.SKILLS, rest[0])
    if not rec:
        return f"No pending skill write with id '{rest[0]}'."
    diff = wa.skill_pending_diff(rec)
    header = f"# Pending skill write {rec['id']}: {rec.get('summary', '')}\n"
    return header + "\n" + diff


def _set_approval(subsystem: str, rest: List[str], set_mode_fn) -> str:
    """Turn the approval gate on/off for a subsystem.

    ``set_mode_fn`` (when provided) persists the new boolean to config.
    """
    if not rest:
        return (f"{_fmt_state(subsystem)}\n"
                f"Set with: /{subsystem} approval <on|off>")
    arg = rest[0].strip().lower()
    truthy = {"on", "true", "yes", "1", "enable", "enabled"}
    falsey = {"off", "false", "no", "0", "disable", "disabled"}
    if arg in truthy:
        enabled = True
    elif arg in falsey:
        enabled = False
    else:
        return f"Invalid value '{arg}'. Use: on or off."
    if set_mode_fn is None:
        val = "true" if enabled else "false"
        return (f"To change the {subsystem} approval gate, run:\n"
                f"  hermes config set {subsystem}.write_approval {val}")
    try:
        set_mode_fn(enabled)
    except Exception as e:
        return f"Failed to set {subsystem}.write_approval: {e}"
    return f"{subsystem}.write_approval set to '{'on' if enabled else 'off'}'."
