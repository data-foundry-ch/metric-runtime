"""Durable Postgres runtime store.

Persists observations, metric state, committed evaluations, evaluation
claims, incidents and the notification outbox in a dedicated schema
(default ``metric_runtime``). It never reads or writes analytical source data.

Concurrency model:

- Evaluation claims are leased rows (``expires_at``) so a crashed worker's
  claim can be taken over.
- ``ordered_stream_commit`` only *waits* (no lock held across the yield).
- ``commit()`` takes a transaction-scoped advisory lock per (metric, scope)
  stream (``pg_advisory_xact_lock``), re-checks claim / duplicate / staleness
  / earlier claims while holding it, and guards the metric-state upsert with
  the expected version. The lock is released by COMMIT/ROLLBACK, so it can
  never leak onto a pooled connection.
- Outbox leases are persisted (``claim_token`` / ``claimed_until``);
  ``FOR UPDATE SKIP LOCKED`` only arbitrates the claiming statement itself.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from typing import Any

from metric_runtime.adapters.postgres.migrations import (
    Migration,
    applied_migrations,
    migrate,
    pending_migrations,
    validate_schema_name,
)
from metric_runtime.exceptions import (
    EvaluationInProgressError,
    MetricRuntimeError,
    NotificationLeaseLostError,
    RuntimeStoreNotMigratedError,
    StaleEvaluationError,
    StreamCommitConflict,
)
from metric_runtime.identity import EvaluationKey, canonical_scope_key, ensure_utc
from metric_runtime.migrations import SchemaStatus
from metric_runtime.models import (
    EvaluationRecord,
    Incident,
    KPIState,
    MetricStateRecord,
    OutboxEvent,
    StoredObservation,
)
from metric_runtime.stores.base import EvaluationClaim, EvaluationClaimStatus
from metric_runtime.stores.staging import (
    ACTIVE_INCIDENT_STATES,
    StagedTransaction,
    latest_incident,
)

__all__ = ["PostgresRuntimeStore"]

_ACTIVE_STATES_SQL = tuple(s.value for s in ACTIVE_INCIDENT_STATES)

_OUTBOX_COLUMNS = (
    "id, event_key, created_at, delivered_at, dead_lettered_at, attempt_count, "
    "last_attempt_at, last_error, next_attempt_at, claim_token, claimed_until, payload"
)


def _require_psycopg() -> tuple[Any, Any]:
    try:
        import psycopg
        import psycopg_pool
    except ImportError as exc:  # pragma: no cover - optional dependency
        raise MetricRuntimeError(
            'Postgres runtime store requires: pip install "metric-runtime[postgres]"'
        ) from exc
    return psycopg, psycopg_pool


def _jsonb(model: Any) -> Any:
    from psycopg.types.json import Jsonb

    return Jsonb(model.model_dump(mode="json"))


def _stream_lock_key(metric: str, scope_key: str) -> str:
    return f"metric_runtime.stream:{metric}|{scope_key}"


def _outbox_from_row(row: tuple[Any, ...]) -> OutboxEvent:
    (
        event_id,
        event_key,
        created_at,
        delivered_at,
        dead_lettered_at,
        attempt_count,
        last_attempt_at,
        last_error,
        next_attempt_at,
        claim_token,
        claimed_until,
        payload,
    ) = row
    data = dict(payload)
    data.update(
        {
            "id": event_id,
            "event_key": event_key,
            "created_at": created_at,
            "delivered_at": delivered_at,
            "dead_lettered_at": dead_lettered_at,
            "attempt_count": attempt_count,
            "last_attempt_at": last_attempt_at,
            "last_error": last_error,
            "next_attempt_at": next_attempt_at,
            "claim_token": claim_token,
            "claimed_until": claimed_until,
        }
    )
    return OutboxEvent.model_validate(data)


class _PostgresTransaction(StagedTransaction):
    def __init__(
        self,
        store: PostgresRuntimeStore,
        *,
        evaluation_key: EvaluationKey | None = None,
        claim_token: str | None = None,
    ) -> None:
        super().__init__()
        self._store = store
        self._evaluation_key = evaluation_key
        self._claim_token = claim_token

    def _committed_observation(self, key: EvaluationKey) -> StoredObservation | None:
        return self._store.get_observation(key)

    def _committed_result(self, key: EvaluationKey) -> EvaluationRecord | None:
        return self._store.get_committed_result(key)

    def _committed_history(self, metric: str, scope_key: str) -> list[StoredObservation]:
        return self._store.get_history(metric, scope_key)

    def _committed_state(self, metric: str, scope_key: str) -> MetricStateRecord:
        return self._store.get_state_record(metric, scope_key)

    def _committed_active_incidents(self, metric: str, scope_key: str) -> list[Incident]:
        return self._store._active_incidents(metric, scope_key)

    def _committed_incident(self, incident_id: str) -> Incident | None:
        return self._store.get_incident(incident_id)

    def _committed_notification_by_key(self, event_key: str) -> OutboxEvent | None:
        return self._store._notification_by_key(event_key)

    def _allocate_incident_id(self) -> str:
        return f"inc-{self._store._nextval('incident_seq'):04d}"

    def _allocate_outbox_id(self) -> str:
        return f"out-{self._store._nextval('outbox_seq'):04d}"

    def _apply(self) -> None:
        store = self._store
        key = self._evaluation_key
        streams = {(s.metric, s.scope_key) for s in self._states.values()}
        if key is not None:
            streams.add((key.metric, key.scope_key))
        with store._connection() as conn, conn.transaction():
            for metric, scope_key in sorted(streams):
                conn.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                    (_stream_lock_key(metric, scope_key),),
                )
            if key is not None:
                self._recheck_under_lock(conn, key)
            for record in self._states.values():
                self._write_state(conn, record)
            for obs_id, obs in self._obs.items():
                conn.execute(
                    """
                    INSERT INTO observations
                        (identity, metric, scope_key, eval_at, recorded_at, payload)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    ON CONFLICT (identity) DO NOTHING
                    """,
                    (
                        obs_id,
                        obs.key.metric,
                        obs.key.scope_key,
                        obs.key.eval_at,
                        obs.recorded_at,
                        _jsonb(obs),
                    ),
                )
            for incident in self._incidents.values():
                conn.execute(
                    """
                    INSERT INTO incidents (id, primary_metric, scope_key, state, updated_at, payload)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    ON CONFLICT (id) DO UPDATE SET
                        primary_metric = EXCLUDED.primary_metric,
                        scope_key = EXCLUDED.scope_key,
                        state = EXCLUDED.state,
                        updated_at = EXCLUDED.updated_at,
                        payload = EXCLUDED.payload
                    """,
                    (
                        incident.id,
                        incident.primary_metric,
                        canonical_scope_key(incident.scope),
                        incident.state.value,
                        incident.updated_at,
                        _jsonb(incident),
                    ),
                )
            for event in self._outbox.values():
                conn.execute(
                    """
                    INSERT INTO outbox (
                        id, event_key, created_at, delivered_at, dead_lettered_at,
                        attempt_count, last_attempt_at, last_error, next_attempt_at, payload
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (event_key) WHERE event_key <> '' DO NOTHING
                    """,
                    (
                        event.id,
                        event.event_key,
                        event.created_at,
                        event.delivered_at,
                        event.dead_lettered_at,
                        event.attempt_count,
                        event.last_attempt_at,
                        event.last_error,
                        event.next_attempt_at,
                        _jsonb(event),
                    ),
                )
            for identity, evaluation in self._evaluations.items():
                conn.execute(
                    """
                    INSERT INTO evaluations
                        (identity, metric, scope_key, effective_at, committed_at, payload)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    """,
                    (
                        identity,
                        evaluation.key.metric,
                        evaluation.key.scope_key,
                        evaluation.effective_at or evaluation.key.eval_at,
                        evaluation.committed_at,
                        _jsonb(evaluation),
                    ),
                )
            if callable(store.commit_hook):
                store.commit_hook(self)

    def _recheck_under_lock(self, conn: Any, key: EvaluationKey) -> None:
        if self._claim_token is not None:
            row = conn.execute(
                "SELECT token FROM evaluation_claims WHERE identity = %s",
                (key.identity,),
            ).fetchone()
            if row is None or row[0] != self._claim_token:
                raise EvaluationInProgressError(
                    f"Evaluation claim for {key.identity} was lost before commit"
                )
        if conn.execute(
            "SELECT 1 FROM evaluations WHERE identity = %s", (key.identity,)
        ).fetchone():
            raise EvaluationInProgressError(f"Evaluation {key.identity} is already committed")
        row = conn.execute(
            "SELECT last_evaluation_at FROM metric_state WHERE metric = %s AND scope_key = %s",
            (key.metric, key.scope_key),
        ).fetchone()
        effective = ensure_utc(key.eval_at)
        if row is not None and row[0] is not None and ensure_utc(row[0]) >= effective:
            raise StaleEvaluationError(
                f"Stale evaluation for {key.metric} scope={key.scope_key}: "
                f"effective_at={effective.isoformat()} is not after "
                f"last_evaluation_at={ensure_utc(row[0]).isoformat()}"
            )
        earlier = conn.execute(
            """
            SELECT 1 FROM evaluation_claims
            WHERE metric = %s AND scope_key = %s AND eval_at < %s
              AND identity <> %s AND expires_at > now()
            LIMIT 1
            """,
            (key.metric, key.scope_key, effective, key.identity),
        ).fetchone()
        if earlier:
            raise StreamCommitConflict(
                f"An earlier evaluation on {key.metric}/{key.scope_key} is still in flight"
            )

    @staticmethod
    def _write_state(conn: Any, record: MetricStateRecord) -> None:
        cur = conn.execute(
            """
            INSERT INTO metric_state
                (metric, scope_key, version, state, last_evaluation_at, updated_at, payload)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (metric, scope_key) DO UPDATE SET
                version = EXCLUDED.version,
                state = EXCLUDED.state,
                last_evaluation_at = EXCLUDED.last_evaluation_at,
                updated_at = EXCLUDED.updated_at,
                payload = EXCLUDED.payload
            WHERE metric_state.version = EXCLUDED.version - 1
            """,
            (
                record.metric,
                record.scope_key,
                record.version,
                record.state.value,
                record.last_evaluation_at,
                record.updated_at,
                _jsonb(record),
            ),
        )
        if cur.rowcount != 1:
            raise StreamCommitConflict(
                f"Metric state version conflict for {record.metric}/{record.scope_key}: "
                f"could not apply version {record.version}"
            )


class PostgresRuntimeStore:
    """Transactional runtime store backed by Postgres (psycopg 3 + pool)."""

    def __init__(
        self,
        conninfo: str = "",
        *,
        schema: str = "metric_runtime",
        pool: Any | None = None,
        min_size: int = 1,
        max_size: int = 10,
        claim_ttl: float = 900.0,
        stream_poll_interval: float = 0.05,
        stream_poll_max: float = 1.0,
        connect_kwargs: dict[str, Any] | None = None,
    ) -> None:
        psycopg, psycopg_pool = _require_psycopg()
        self.schema = validate_schema_name(schema)
        self.claim_ttl = float(claim_ttl)
        self.stream_poll_interval = stream_poll_interval
        self.stream_poll_max = stream_poll_max
        self.commit_hook = None
        self._conninfo = conninfo
        self._connect_kwargs = dict(connect_kwargs or {})
        if pool is not None:
            self._pool = pool
            self._owns_pool = False
        else:
            self._pool = psycopg_pool.ConnectionPool(
                conninfo,
                min_size=min_size,
                max_size=max_size,
                kwargs={**self._connect_kwargs, "autocommit": True},
                configure=self._configure,
                open=True,
            )
            self._owns_pool = True

    # --- connections & schema ------------------------------------------------------

    def _configure(self, conn: Any) -> None:
        from psycopg import sql

        conn.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(self.schema)))

    @contextmanager
    def _connection(self) -> Iterator[Any]:
        with self._pool.connection() as conn:
            yield conn

    @contextmanager
    def _admin_connection(self) -> Iterator[Any]:
        """Dedicated autocommit connection (migrations)."""
        psycopg, _ = _require_psycopg()
        with psycopg.connect(self._conninfo, autocommit=True, **self._connect_kwargs) as conn:
            yield conn

    def migrate(self, migrations: list[Migration] | None = None) -> list[Migration]:
        with self._admin_connection() as conn:
            return migrate(conn, self.schema, migrations)

    def pending_migrations(self, migrations: list[Migration] | None = None) -> list[Migration]:
        with self._connection() as conn:
            return pending_migrations(conn, self.schema, migrations)

    def applied_migrations(self) -> dict[int, tuple[str, str]]:
        """``{version: (name, checksum)}`` recorded in ``schema_migrations``."""
        with self._connection() as conn:
            return applied_migrations(conn, self.schema)

    def ensure_migrated(self) -> None:
        pending = self.pending_migrations()
        if pending:
            names = ", ".join(m.filename for m in pending)
            raise RuntimeStoreNotMigratedError(
                f"Runtime store schema {self.schema!r} has pending migrations ({names}). "
                "Run: metric-runtime store migrate --profile <profile>"
            )

    # --- ManagedRuntimeStore ---------------------------------------------------------

    @property
    def namespace(self) -> str:
        return f"schema {self.schema}"

    def schema_status(self) -> SchemaStatus:
        with self._connection() as conn:
            applied = applied_migrations(conn, self.schema)
            pending = pending_migrations(conn, self.schema)
        return SchemaStatus(namespace=self.namespace, applied=applied, pending=pending)

    def ensure_ready(self) -> None:
        self.ensure_migrated()

    def close(self) -> None:
        if self._owns_pool:
            self._pool.close()

    def _nextval(self, sequence: str) -> int:
        with self._connection() as conn:
            row = conn.execute("SELECT nextval(%s::regclass)", (sequence,)).fetchone()
        assert row is not None
        return int(row[0])

    @staticmethod
    def _db_now(conn: Any) -> datetime:
        row = conn.execute("SELECT now()").fetchone()
        assert row is not None
        return ensure_utc(row[0])

    # --- observations --------------------------------------------------------------

    def get_observation(self, key: EvaluationKey) -> StoredObservation | None:
        with self._connection() as conn:
            row = conn.execute(
                "SELECT payload FROM observations WHERE identity = %s", (key.identity,)
            ).fetchone()
        return StoredObservation.model_validate(row[0]) if row else None

    def put_observation(self, observation: StoredObservation) -> StoredObservation:
        with self.transaction() as tx:
            tx.stage_observation(observation)
            tx.commit()
        return observation

    def get_history(self, metric: str, scope_key: str = "") -> list[StoredObservation]:
        with self._connection() as conn:
            rows = conn.execute(
                """
                SELECT payload FROM observations
                WHERE metric = %s AND scope_key = %s
                ORDER BY eval_at, recorded_at
                """,
                (metric, scope_key),
            ).fetchall()
        return [StoredObservation.model_validate(r[0]) for r in rows]

    # --- metric state --------------------------------------------------------------

    def get_state_record(self, metric: str, scope_key: str = "") -> MetricStateRecord:
        with self._connection() as conn:
            row = conn.execute(
                "SELECT payload FROM metric_state WHERE metric = %s AND scope_key = %s",
                (metric, scope_key),
            ).fetchone()
        if row is not None:
            return MetricStateRecord.model_validate(row[0])
        now = datetime.now(UTC)
        return MetricStateRecord(
            metric=metric,
            scope_key=scope_key,
            state=KPIState.NORMAL,
            state_since=now,
            updated_at=now,
            last_evaluation_at=None,
            version=0,
        )

    def set_state_record(self, record: MetricStateRecord) -> None:
        with self.transaction() as tx:
            tx.stage_state_record(record)
            tx.commit()

    def get_state(self, metric: str, scope_key: str = "") -> KPIState:
        return self.get_state_record(metric, scope_key).state

    # --- incidents -----------------------------------------------------------------

    def get_incident(self, incident_id: str) -> Incident | None:
        with self._connection() as conn:
            row = conn.execute(
                "SELECT payload FROM incidents WHERE id = %s", (incident_id,)
            ).fetchone()
        return Incident.model_validate(row[0]) if row else None

    def upsert_incident(self, incident: Incident) -> Incident:
        with self.transaction() as tx:
            stored = tx.stage_incident(incident)
            tx.commit()
        return stored

    def list_open_incidents(self) -> list[Incident]:
        with self._connection() as conn:
            rows = conn.execute(
                "SELECT payload FROM incidents WHERE state = ANY(%s) ORDER BY id",
                (list(_ACTIVE_STATES_SQL),),
            ).fetchall()
        return [Incident.model_validate(r[0]) for r in rows]

    def _active_incidents(self, metric: str, scope_key: str) -> list[Incident]:
        with self._connection() as conn:
            rows = conn.execute(
                """
                SELECT payload FROM incidents
                WHERE primary_metric = %s AND scope_key = %s AND state = ANY(%s)
                """,
                (metric, scope_key, list(_ACTIVE_STATES_SQL)),
            ).fetchall()
        return [Incident.model_validate(r[0]) for r in rows]

    def find_active_incident(self, metric: str, scope_key: str = "") -> Incident | None:
        return latest_incident(self._active_incidents(metric, scope_key))

    # --- notification outbox -------------------------------------------------------

    def _notification_by_key(self, event_key: str) -> OutboxEvent | None:
        with self._connection() as conn:
            row = conn.execute(
                f"SELECT {_OUTBOX_COLUMNS} FROM outbox WHERE event_key = %s",
                (event_key,),
            ).fetchone()
        return _outbox_from_row(row) if row else None

    def enqueue_notification(self, event: OutboxEvent) -> OutboxEvent:
        with self.transaction() as tx:
            stored = tx.stage_notification(event)
            tx.commit()
        return stored

    def list_pending_notifications(self) -> list[OutboxEvent]:
        with self._connection() as conn:
            rows = conn.execute(
                f"""
                SELECT {_OUTBOX_COLUMNS} FROM outbox
                WHERE delivered_at IS NULL AND dead_lettered_at IS NULL
                ORDER BY created_at, id
                """
            ).fetchall()
        return [_outbox_from_row(r) for r in rows]

    def claim_pending_notifications(
        self,
        *,
        now: datetime,
        limit: int | None = None,
        lease: timedelta,
    ) -> list[OutboxEvent]:
        now = ensure_utc(now)
        token = uuid.uuid4().hex
        with self._connection() as conn:
            rows = conn.execute(
                f"""
                UPDATE outbox SET claim_token = %(token)s, claimed_until = %(until)s
                WHERE id IN (
                    SELECT id FROM outbox
                    WHERE delivered_at IS NULL
                      AND dead_lettered_at IS NULL
                      AND (next_attempt_at IS NULL OR next_attempt_at <= %(now)s)
                      AND (claimed_until IS NULL OR claimed_until <= %(now)s)
                    ORDER BY created_at, id
                    LIMIT %(limit)s
                    FOR UPDATE SKIP LOCKED
                )
                RETURNING {_OUTBOX_COLUMNS}
                """,
                {"token": token, "until": now + lease, "now": now, "limit": limit},
            ).fetchall()
        events = [_outbox_from_row(r) for r in rows]
        events.sort(key=lambda e: (ensure_utc(e.created_at), e.id or ""))
        return events

    def _lease_lost_or_missing(self, conn: Any, event_id: str) -> None:
        exists = conn.execute("SELECT 1 FROM outbox WHERE id = %s", (event_id,)).fetchone()
        if not exists:
            raise KeyError(event_id)
        raise NotificationLeaseLostError(
            f"Outbox event {event_id} lease is no longer held by this worker"
        )

    def mark_notification_delivered(
        self,
        event_id: str,
        *,
        at: datetime,
        claim_token: str | None = None,
    ) -> OutboxEvent:
        at = ensure_utc(at)
        with self._connection() as conn:
            row = conn.execute(
                f"""
                UPDATE outbox SET
                    delivered_at = %(at)s,
                    last_attempt_at = %(at)s,
                    attempt_count = attempt_count + 1,
                    last_error = NULL,
                    claim_token = NULL,
                    claimed_until = NULL
                WHERE id = %(id)s
                  AND (%(token)s::text IS NULL OR claim_token = %(token)s::text)
                RETURNING {_OUTBOX_COLUMNS}
                """,
                {"at": at, "id": event_id, "token": claim_token},
            ).fetchone()
            if row is None:
                self._lease_lost_or_missing(conn, event_id)
        assert row is not None
        return _outbox_from_row(row)

    def record_notification_attempt(
        self,
        event_id: str,
        *,
        at: datetime,
        error: str | None = None,
        claim_token: str | None = None,
        next_attempt_at: datetime | None = None,
        dead_letter: bool = False,
    ) -> OutboxEvent:
        at = ensure_utc(at)
        with self._connection() as conn:
            row = conn.execute(
                f"""
                UPDATE outbox SET
                    last_attempt_at = %(at)s,
                    attempt_count = attempt_count + 1,
                    last_error = %(error)s,
                    next_attempt_at = %(next)s,
                    dead_lettered_at = %(dead)s,
                    claim_token = NULL,
                    claimed_until = NULL
                WHERE id = %(id)s
                  AND (%(token)s::text IS NULL OR claim_token = %(token)s::text)
                RETURNING {_OUTBOX_COLUMNS}
                """,
                {
                    "at": at,
                    "error": error,
                    "next": ensure_utc(next_attempt_at) if next_attempt_at else None,
                    "dead": at if dead_letter else None,
                    "id": event_id,
                    "token": claim_token,
                },
            ).fetchone()
            if row is None:
                self._lease_lost_or_missing(conn, event_id)
        assert row is not None
        return _outbox_from_row(row)

    def release_notification_claim(self, event_id: str, *, claim_token: str) -> None:
        with self._connection() as conn:
            conn.execute(
                """
                UPDATE outbox SET claim_token = NULL, claimed_until = NULL
                WHERE id = %s AND claim_token = %s
                """,
                (event_id, claim_token),
            )

    def next_notification_due_at(self, now: datetime) -> datetime | None:
        now = ensure_utc(now)
        with self._connection() as conn:
            row = conn.execute(
                """
                SELECT MIN(GREATEST(%(now)s, next_attempt_at, claimed_until)) FROM outbox
                WHERE delivered_at IS NULL AND dead_lettered_at IS NULL
                """,
                {"now": now},
            ).fetchone()
        if row is None or row[0] is None:
            return None
        return ensure_utc(row[0])

    # --- evaluations, claims and ordering -----------------------------------------

    def get_committed_result(self, key: EvaluationKey) -> EvaluationRecord | None:
        with self._connection() as conn:
            row = conn.execute(
                "SELECT payload FROM evaluations WHERE identity = %s", (key.identity,)
            ).fetchone()
        return EvaluationRecord.model_validate(row[0]) if row else None

    def latest_committed_evaluation(
        self,
        metric: str,
        scope_key: str | None = None,
    ) -> EvaluationRecord | None:
        if scope_key is None:
            scope_key = canonical_scope_key()
        with self._connection() as conn:
            row = conn.execute(
                """
                SELECT payload FROM evaluations
                WHERE metric = %s AND scope_key = %s
                ORDER BY effective_at DESC
                LIMIT 1
                """,
                (metric, scope_key),
            ).fetchone()
        return EvaluationRecord.model_validate(row[0]) if row else None

    def claim_evaluation(self, key: EvaluationKey) -> EvaluationClaim:
        token = uuid.uuid4().hex
        with self._connection() as conn, conn.transaction():
            row = conn.execute(
                "SELECT payload FROM evaluations WHERE identity = %s", (key.identity,)
            ).fetchone()
            if row is not None:
                return EvaluationClaim(
                    EvaluationClaimStatus.ALREADY_COMMITTED,
                    key=key,
                    record=EvaluationRecord.model_validate(row[0]),
                )
            conn.execute(
                "DELETE FROM evaluation_claims WHERE identity = %s AND expires_at <= now()",
                (key.identity,),
            )
            inserted = conn.execute(
                """
                INSERT INTO evaluation_claims
                    (identity, metric, scope_key, eval_at, token, claimed_at, expires_at)
                VALUES (%s, %s, %s, %s, %s, now(), now() + %s)
                ON CONFLICT (identity) DO NOTHING
                RETURNING token
                """,
                (
                    key.identity,
                    key.metric,
                    key.scope_key,
                    key.eval_at,
                    token,
                    timedelta(seconds=self.claim_ttl),
                ),
            ).fetchone()
            if inserted is None:
                return EvaluationClaim(EvaluationClaimStatus.IN_PROGRESS, key=key)
            # A worker may have committed and released between our first read
            # and the insert; READ COMMITTED lets this statement see it.
            row = conn.execute(
                "SELECT payload FROM evaluations WHERE identity = %s", (key.identity,)
            ).fetchone()
            if row is not None:
                conn.execute("DELETE FROM evaluation_claims WHERE identity = %s", (key.identity,))
                return EvaluationClaim(
                    EvaluationClaimStatus.ALREADY_COMMITTED,
                    key=key,
                    record=EvaluationRecord.model_validate(row[0]),
                )
        return EvaluationClaim(EvaluationClaimStatus.ACQUIRED, key=key, token=token)

    def release_evaluation_claim(self, key: EvaluationKey, *, token: str | None = None) -> None:
        with self._connection() as conn:
            if token is None:
                conn.execute("DELETE FROM evaluation_claims WHERE identity = %s", (key.identity,))
            else:
                conn.execute(
                    "DELETE FROM evaluation_claims WHERE identity = %s AND token = %s",
                    (key.identity, token),
                )

    @contextmanager
    def ordered_stream_commit(
        self,
        key: EvaluationKey,
        *,
        timeout: float | None = 30.0,
    ) -> Iterator[None]:
        """Wait (without holding a lock) until no earlier claim is in flight.

        The authoritative ordering check happens again inside ``commit()``
        under the transaction-scoped stream lock.
        """
        effective = ensure_utc(key.eval_at)
        deadline = None if timeout is None else time.monotonic() + timeout
        delay = self.stream_poll_interval
        while True:
            with self._connection() as conn:
                row = conn.execute(
                    """
                    SELECT last_evaluation_at FROM metric_state
                    WHERE metric = %s AND scope_key = %s
                    """,
                    (key.metric, key.scope_key),
                ).fetchone()
                if row is not None and row[0] is not None and ensure_utc(row[0]) >= effective:
                    raise StaleEvaluationError(
                        f"Stale evaluation for {key.metric} scope={key.scope_key}: "
                        f"effective_at={effective.isoformat()} is not after "
                        f"last_evaluation_at={ensure_utc(row[0]).isoformat()}"
                    )
                earlier = conn.execute(
                    """
                    SELECT 1 FROM evaluation_claims
                    WHERE metric = %s AND scope_key = %s AND eval_at < %s
                      AND identity <> %s AND expires_at > now()
                    LIMIT 1
                    """,
                    (key.metric, key.scope_key, effective, key.identity),
                ).fetchone()
            if not earlier:
                break
            if deadline is not None and time.monotonic() >= deadline:
                raise EvaluationInProgressError(
                    f"Timed out waiting for earlier evaluations on "
                    f"{key.metric}/{key.scope_key} before {effective.isoformat()}"
                )
            time.sleep(delay)
            delay = min(delay * 2, self.stream_poll_max)
        yield

    @contextmanager
    def transaction(
        self,
        *,
        evaluation_key: EvaluationKey | None = None,
        claim_token: str | None = None,
    ) -> Iterator[_PostgresTransaction]:
        tx = _PostgresTransaction(self, evaluation_key=evaluation_key, claim_token=claim_token)
        try:
            yield tx
            if not tx.closed:
                tx.rollback()
        except Exception:
            if not tx._committed:
                tx.rollback()
            raise
