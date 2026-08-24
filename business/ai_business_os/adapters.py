from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping

from .contracts import EvidenceEnvelope


def _coerce_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)


@dataclass(frozen=True)
class ReadOnlyAdapter:
    """A read-only business adapter that can only inspect snapshots."""

    adapter_id: str
    source: str
    freshness_limit_hours: int = 24
    required_fields: tuple[str, ...] = ()
    read_only: bool = True

    def inspect(
        self,
        snapshot: Mapping[str, Any] | None,
        *,
        now: datetime | None = None,
    ) -> EvidenceEnvelope:
        now = now or datetime.now(timezone.utc)
        if snapshot is None:
            return EvidenceEnvelope(
                adapter_id=self.adapter_id,
                source=self.source,
                observed_at=None,
                freshness="missing",
                notes="snapshot missing",
                payload={},
            )

        payload = dict(snapshot)
        observed_at = _coerce_datetime(payload.get("observed_at"))
        if observed_at is None:
            return EvidenceEnvelope(
                adapter_id=self.adapter_id,
                source=self.source,
                observed_at=None,
                freshness="missing",
                notes="snapshot missing observed_at",
                payload=payload,
            )

        missing_fields = [field for field in self.required_fields if field not in payload]
        if missing_fields:
            return EvidenceEnvelope(
                adapter_id=self.adapter_id,
                source=self.source,
                observed_at=observed_at,
                freshness="missing",
                notes=f"missing required fields: {', '.join(missing_fields)}",
                payload=payload,
            )

        age = now - observed_at
        if age > timedelta(hours=self.freshness_limit_hours):
            return EvidenceEnvelope(
                adapter_id=self.adapter_id,
                source=self.source,
                observed_at=observed_at,
                freshness="expired",
                notes=(
                    f"snapshot older than {self.freshness_limit_hours}h "
                    f"(age={age.total_seconds() / 3600:.1f}h)"
                ),
                payload=payload,
            )

        return EvidenceEnvelope(
            adapter_id=self.adapter_id,
            source=self.source,
            observed_at=observed_at,
            freshness="fresh",
            notes="snapshot is fresh",
            payload=payload,
        )
