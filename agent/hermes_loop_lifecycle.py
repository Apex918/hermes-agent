"""Offline Hermes Loop task lifecycle and evidence ledger.

The loop is intentionally small and local-only.  It provides a durable,
append-only audit trail for a task without importing the gateway, dispatcher,
cron, credentials, or any network client.  A caller supplies a SQLite path
(usually a test fixture path); no default global database is opened.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from enum import Enum
from pathlib import Path
from typing import Any, Optional, Union


class LifecycleTransitionError(ValueError):
    """Raised when a task attempts a transition outside the loop contract."""


class LifecycleState(str, Enum):
    """Durable states in the Hermes Loop contract."""

    CREATED = "CREATED"
    PLANNED = "PLANNED"
    DISPATCHED = "DISPATCHED"
    RUNNING = "RUNNING"
    RESULT_RECEIVED = "RESULT_RECEIVED"
    VERIFIED = "VERIFIED"
    EXPERIENCE_CAPTURED = "EXPERIENCE_CAPTURED"
    CLOSED = "CLOSED"
    TIMEOUT = "TIMEOUT"
    FAILED = "FAILED"
    VERIFICATION_FAILED = "VERIFICATION_FAILED"
    REPLAN = "REPLAN"


# Failure states are deliberately recoverable only through REPLAN.  In
# particular, a failed result cannot jump straight to VERIFIED or CLOSED.
_ALLOWED_TRANSITIONS = {
    LifecycleState.CREATED: frozenset({LifecycleState.PLANNED}),
    LifecycleState.PLANNED: frozenset({LifecycleState.DISPATCHED}),
    LifecycleState.DISPATCHED: frozenset(
        {LifecycleState.RUNNING, LifecycleState.TIMEOUT, LifecycleState.FAILED}
    ),
    LifecycleState.RUNNING: frozenset(
        {LifecycleState.RESULT_RECEIVED, LifecycleState.TIMEOUT, LifecycleState.FAILED}
    ),
    LifecycleState.RESULT_RECEIVED: frozenset(
        {LifecycleState.VERIFIED, LifecycleState.VERIFICATION_FAILED}
    ),
    LifecycleState.VERIFIED: frozenset({LifecycleState.EXPERIENCE_CAPTURED}),
    LifecycleState.EXPERIENCE_CAPTURED: frozenset({LifecycleState.CLOSED}),
    LifecycleState.CLOSED: frozenset(),
    LifecycleState.TIMEOUT: frozenset({LifecycleState.REPLAN}),
    LifecycleState.FAILED: frozenset({LifecycleState.REPLAN}),
    LifecycleState.VERIFICATION_FAILED: frozenset({LifecycleState.REPLAN}),
    LifecycleState.REPLAN: frozenset({LifecycleState.PLANNED}),
}
_FAILURE_STATES = frozenset(
    {
        LifecycleState.TIMEOUT,
        LifecycleState.FAILED,
        LifecycleState.VERIFICATION_FAILED,
    }
)


def _as_state(value: Union[LifecycleState, str]) -> LifecycleState:
    if isinstance(value, LifecycleState):
        return value
    try:
        return LifecycleState(str(value).upper())
    except ValueError as exc:
        raise LifecycleTransitionError("unknown lifecycle state: %r" % (value,)) from exc


def _json_object(value: Any, field: str) -> dict[str, Any]:
    """Validate and copy a JSON object, rejecting repr-like payloads."""
    if not isinstance(value, Mapping):
        raise TypeError("%s must be a JSON object" % field)
    copied = dict(value)
    try:
        json.dumps(copied, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError) as exc:
        raise TypeError("%s must contain JSON-serializable values" % field) from exc
    return copied


def _json_text(value: Any, field: str) -> str:
    obj = _json_object(value, field)
    return json.dumps(obj, sort_keys=True, separators=(",", ":"))


def _evidence_items(value: Optional[Sequence[Mapping[str, Any]]]) -> list[dict[str, Any]]:
    if value is None:
        return []
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise TypeError("evidence must be a sequence of JSON objects")
    items = []
    for index, item in enumerate(value):
        items.append(_json_object(item, "evidence[%d]" % index))
    return items


def _content_hash(payload: Mapping[str, Any]) -> str:
    canonical = json.dumps(dict(payload), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class HermesLoopStore:
    """SQLite-backed, local-only lifecycle store.

    The store is safe to use by multiple threads sharing this instance.  Each
    public mutation commits its state transition and associated evidence in one
    SQLite transaction, so an audit can never observe a result without the
    evidence that accompanied it.
    """

    def __init__(self, db_path: Union[str, Path], *, clock: Callable[[], float] = time.time):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._clock = clock
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._initialize()

    def _initialize(self) -> None:
        with self._conn:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS loop_tasks (
                    task_id TEXT PRIMARY KEY,
                    objective TEXT NOT NULL,
                    state TEXT NOT NULL,
                    metadata_json TEXT NOT NULL,
                    plan_json TEXT,
                    result_json TEXT,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    revision INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS loop_events (
                    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id TEXT NOT NULL REFERENCES loop_tasks(task_id),
                    from_state TEXT,
                    to_state TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS loop_evidence (
                    evidence_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id TEXT NOT NULL REFERENCES loop_tasks(task_id),
                    stage TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    content_hash TEXT NOT NULL,
                    recorded_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS loop_experiences (
                    experience_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id TEXT NOT NULL REFERENCES loop_tasks(task_id),
                    payload_json TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    captured_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS loop_events_task_idx
                    ON loop_events(task_id, event_id);
                CREATE INDEX IF NOT EXISTS loop_evidence_task_idx
                    ON loop_evidence(task_id, evidence_id);
                """
            )

    def close_db(self) -> None:
        """Close the local SQLite connection."""
        with self._lock:
            self._conn.close()

    def __enter__(self) -> "HermesLoopStore":
        return self

    def __exit__(self, _type: Any, _value: Any, _traceback: Any) -> None:
        self.close_db()

    def create_task(
        self,
        objective: str,
        *,
        metadata: Optional[Mapping[str, Any]] = None,
        task_id: Optional[str] = None,
    ) -> str:
        if not isinstance(objective, str) or not objective.strip():
            raise ValueError("objective must be a non-empty string")
        metadata_obj = _json_object(
            metadata if metadata is not None else {}, "metadata"
        )
        task_id = task_id or "loop-" + uuid.uuid4().hex
        now = self._clock()
        with self._lock, self._conn:
            self._conn.execute(
                """
                INSERT INTO loop_tasks
                    (task_id, objective, state, metadata_json, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    task_id,
                    objective.strip(),
                    LifecycleState.CREATED.value,
                    _json_text(metadata_obj, "metadata"),
                    now,
                    now,
                ),
            )
            self._insert_event(
                task_id,
                None,
                LifecycleState.CREATED,
                "created",
                "coordinator",
                metadata_obj,
                now,
            )
        return task_id

    def state(self, task_id: str) -> LifecycleState:
        with self._lock:
            row = self._task_row(task_id)
            return _as_state(row["state"])

    def current_state(self, task_id: str) -> LifecycleState:
        """Alias useful to callers that treat the store as a state machine."""
        return self.state(task_id)

    def plan(self, task_id: str, *, plan: Mapping[str, Any], actor: str = "planner") -> None:
        plan_obj = _json_object(plan, "plan")
        with self._lock, self._conn:
            self._transition_locked(
                task_id,
                LifecycleState.PLANNED,
                actor=actor,
                payload={"plan": plan_obj},
            )
            self._conn.execute(
                "UPDATE loop_tasks SET plan_json = ? WHERE task_id = ?",
                (_json_text(plan_obj, "plan"), task_id),
            )

    def dispatch(self, task_id: str, *, worker: str, actor: str = "dispatcher") -> None:
        if not isinstance(worker, str) or not worker.strip():
            raise ValueError("worker must be a non-empty string")
        self.transition(
            task_id,
            LifecycleState.DISPATCHED,
            actor=actor,
            payload={"worker": worker.strip()},
        )

    def start(self, task_id: str, *, actor: str = "worker") -> None:
        self.transition(task_id, LifecycleState.RUNNING, actor=actor)

    def record_result(
        self,
        task_id: str,
        *,
        result: Mapping[str, Any],
        evidence: Optional[Sequence[Mapping[str, Any]]] = None,
        actor: str = "worker",
    ) -> None:
        result_obj = _json_object(result, "result")
        evidence_items = _evidence_items(evidence)
        with self._lock, self._conn:
            self._transition_locked(
                task_id,
                LifecycleState.RESULT_RECEIVED,
                actor=actor,
                payload={"result": result_obj, "evidence_count": len(evidence_items)},
            )
            self._conn.execute(
                "UPDATE loop_tasks SET result_json = ? WHERE task_id = ?",
                (_json_text(result_obj, "result"), task_id),
            )
            self._insert_evidence_locked(task_id, "result", evidence_items)

    def verify(
        self,
        task_id: str,
        *,
        passed: bool,
        evidence: Optional[Sequence[Mapping[str, Any]]] = None,
        verifier: str = "verifier",
        reason: Optional[str] = None,
    ) -> None:
        if not isinstance(passed, bool):
            raise TypeError("passed must be a boolean")
        if not isinstance(verifier, str) or not verifier.strip():
            raise ValueError("verifier must be a non-empty string")
        evidence_items = _evidence_items(evidence)
        if passed and not evidence_items:
            raise ValueError("successful verification requires evidence")
        target = LifecycleState.VERIFIED if passed else LifecycleState.VERIFICATION_FAILED
        payload: dict[str, Any] = {
            "passed": passed,
            "verifier": verifier.strip(),
            "evidence_count": len(evidence_items),
        }
        if reason is not None:
            payload["reason"] = str(reason)
        with self._lock, self._conn:
            self._transition_locked(task_id, target, actor=verifier.strip(), payload=payload)
            self._insert_evidence_locked(task_id, "verification", evidence_items)

    def capture_experience(
        self,
        task_id: str,
        *,
        experience: Mapping[str, Any],
        actor: str = "curator",
    ) -> None:
        experience_obj = _json_object(experience, "experience")
        if not isinstance(actor, str) or not actor.strip():
            raise ValueError("actor must be a non-empty string")
        with self._lock, self._conn:
            self._transition_locked(
                task_id,
                LifecycleState.EXPERIENCE_CAPTURED,
                actor=actor.strip(),
                payload={"experience": experience_obj},
            )
            now = self._clock()
            self._conn.execute(
                """
                INSERT INTO loop_experiences(task_id, payload_json, actor, captured_at)
                VALUES (?, ?, ?, ?)
                """,
                (task_id, _json_text(experience_obj, "experience"), actor.strip(), now),
            )

    def close(self, task_id: str, *, actor: str = "coordinator") -> None:
        self.transition(task_id, LifecycleState.CLOSED, actor=actor)

    def fail(
        self,
        task_id: str,
        *,
        reason: str,
        kind: Union[LifecycleState, str] = LifecycleState.FAILED,
        evidence: Optional[Sequence[Mapping[str, Any]]] = None,
        actor: str = "worker",
    ) -> None:
        target = _as_state(kind)
        if target not in _FAILURE_STATES:
            raise LifecycleTransitionError("failure kind must be a failure state")
        evidence_items = _evidence_items(evidence)
        with self._lock, self._conn:
            self._transition_locked(
                task_id,
                target,
                actor=actor,
                payload={"reason": str(reason), "evidence_count": len(evidence_items)},
            )
            self._insert_evidence_locked(task_id, target.value.lower(), evidence_items)

    def replan(
        self,
        task_id: str,
        *,
        plan: Mapping[str, Any],
        reason: str,
        actor: str = "planner",
    ) -> None:
        plan_obj = _json_object(plan, "plan")
        with self._lock, self._conn:
            self._transition_locked(
                task_id,
                LifecycleState.REPLAN,
                actor=actor,
                payload={"reason": str(reason), "plan": plan_obj},
            )
            self._conn.execute(
                "UPDATE loop_tasks SET plan_json = ? WHERE task_id = ?",
                (_json_text(plan_obj, "plan"), task_id),
            )

    def transition(
        self,
        task_id: str,
        target: Union[LifecycleState, str],
        *,
        actor: str = "system",
        payload: Optional[Mapping[str, Any]] = None,
    ) -> None:
        target_state = _as_state(target)
        payload_obj = _json_object(
            payload if payload is not None else {}, "payload"
        )
        with self._lock, self._conn:
            self._transition_locked(
                task_id,
                target_state,
                actor=actor,
                payload=payload_obj,
            )

    def audit(self, task_id: str) -> dict[str, Any]:
        """Return a JSON-serializable snapshot of the entire task trail."""
        with self._lock:
            row = self._task_row(task_id)
            events = [self._event_dict(item) for item in self._conn.execute(
                "SELECT * FROM loop_events WHERE task_id = ? ORDER BY event_id", (task_id,)
            )]
            evidence = [self._evidence_dict(item) for item in self._conn.execute(
                "SELECT * FROM loop_evidence WHERE task_id = ? ORDER BY evidence_id", (task_id,)
            )]
            experiences = [self._experience_dict(item) for item in self._conn.execute(
                "SELECT * FROM loop_experiences WHERE task_id = ? ORDER BY experience_id", (task_id,)
            )]
        states = [event["to_state"] for event in events]
        return {
            "task_id": row["task_id"],
            "objective": row["objective"],
            "state": row["state"],
            "metadata": json.loads(row["metadata_json"]),
            "plan": json.loads(row["plan_json"]) if row["plan_json"] else None,
            "result": json.loads(row["result_json"]) if row["result_json"] else None,
            "states": states,
            "failure_states": [state for state in states if state in {item.value for item in _FAILURE_STATES}],
            "events": events,
            "evidence": evidence,
            "evidence_count": len(evidence),
            "experiences": experiences,
            "experience": experiences[-1]["payload"] if experiences else None,
            # This is an explicit contract marker, not a claim that arbitrary
            # callers cannot misuse their own environment outside this store.
            "external_effects": False,
        }

    def events(self, task_id: str) -> list[dict[str, Any]]:
        with self._lock:
            self._task_row(task_id)
            return [self._event_dict(row) for row in self._conn.execute(
                "SELECT * FROM loop_events WHERE task_id = ? ORDER BY event_id", (task_id,)
            )]

    def _task_row(self, task_id: str) -> sqlite3.Row:
        row = self._conn.execute(
            "SELECT * FROM loop_tasks WHERE task_id = ?", (task_id,)
        ).fetchone()
        if row is None:
            raise KeyError("unknown task: %s" % task_id)
        return row

    def _transition_locked(
        self,
        task_id: str,
        target: LifecycleState,
        *,
        actor: str,
        payload: Mapping[str, Any],
    ) -> None:
        if not isinstance(actor, str) or not actor.strip():
            raise ValueError("actor must be a non-empty string")
        row = self._task_row(task_id)
        current = _as_state(row["state"])
        if target not in _ALLOWED_TRANSITIONS[current]:
            raise LifecycleTransitionError(
                "%s -> %s is not allowed" % (current.value, target.value)
            )
        now = self._clock()
        updated = self._conn.execute(
            """
            UPDATE loop_tasks
            SET state = ?, updated_at = ?, revision = revision + 1
            WHERE task_id = ? AND state = ? AND revision = ?
            """,
            (target.value, now, task_id, current.value, row["revision"]),
        )
        if updated.rowcount != 1:
            raise LifecycleTransitionError("concurrent lifecycle update rejected")
        self._insert_event(task_id, current, target, "transition", actor.strip(), payload, now)

    def _insert_event(
        self,
        task_id: str,
        from_state: Optional[LifecycleState],
        to_state: LifecycleState,
        event_type: str,
        actor: str,
        payload: Mapping[str, Any],
        created_at: float,
    ) -> None:
        self._conn.execute(
            """
            INSERT INTO loop_events
                (task_id, from_state, to_state, event_type, actor, payload_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                task_id,
                from_state.value if from_state else None,
                to_state.value,
                event_type,
                actor,
                _json_text(payload, "payload"),
                created_at,
            ),
        )

    def _insert_evidence_locked(
        self,
        task_id: str,
        stage: str,
        evidence_items: Sequence[Mapping[str, Any]],
    ) -> None:
        now = self._clock()
        for item in evidence_items:
            self._conn.execute(
                """
                INSERT INTO loop_evidence
                    (task_id, stage, payload_json, content_hash, recorded_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    task_id,
                    stage,
                    _json_text(item, "evidence"),
                    _content_hash(item),
                    now,
                ),
            )

    @staticmethod
    def _event_dict(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "event_id": row["event_id"],
            "from_state": row["from_state"],
            "to_state": row["to_state"],
            "event_type": row["event_type"],
            "actor": row["actor"],
            "payload": json.loads(row["payload_json"]),
            "created_at": row["created_at"],
        }

    @staticmethod
    def _evidence_dict(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "evidence_id": row["evidence_id"],
            "stage": row["stage"],
            "payload": json.loads(row["payload_json"]),
            "content_hash": row["content_hash"],
            "recorded_at": row["recorded_at"],
        }

    @staticmethod
    def _experience_dict(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "experience_id": row["experience_id"],
            "payload": json.loads(row["payload_json"]),
            "actor": row["actor"],
            "captured_at": row["captured_at"],
        }


__all__ = [
    "HermesLoopStore",
    "LifecycleState",
    "LifecycleTransitionError",
]
