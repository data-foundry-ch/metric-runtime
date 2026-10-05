-- metric-runtime runtime store, schema v1.
-- Unqualified names: the migration runner sets search_path to the runtime schema.

CREATE TABLE observations (
    identity TEXT PRIMARY KEY,
    metric TEXT NOT NULL,
    scope_key TEXT NOT NULL,
    eval_at TIMESTAMPTZ NOT NULL,
    recorded_at TIMESTAMPTZ NOT NULL,
    payload JSONB NOT NULL
);
CREATE INDEX observations_stream_idx ON observations (metric, scope_key, eval_at);

CREATE TABLE metric_state (
    metric TEXT NOT NULL,
    scope_key TEXT NOT NULL,
    version BIGINT NOT NULL,
    state TEXT NOT NULL,
    last_evaluation_at TIMESTAMPTZ,
    updated_at TIMESTAMPTZ NOT NULL,
    payload JSONB NOT NULL,
    PRIMARY KEY (metric, scope_key)
);

CREATE TABLE evaluations (
    identity TEXT PRIMARY KEY,
    metric TEXT NOT NULL,
    scope_key TEXT NOT NULL,
    effective_at TIMESTAMPTZ NOT NULL,
    committed_at TIMESTAMPTZ NOT NULL,
    payload JSONB NOT NULL
);
CREATE INDEX evaluations_cursor_idx ON evaluations (metric, scope_key, effective_at DESC);

CREATE TABLE evaluation_claims (
    identity TEXT PRIMARY KEY,
    metric TEXT NOT NULL,
    scope_key TEXT NOT NULL,
    eval_at TIMESTAMPTZ NOT NULL,
    token TEXT NOT NULL,
    claimed_at TIMESTAMPTZ NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL
);
CREATE INDEX evaluation_claims_stream_idx ON evaluation_claims (metric, scope_key, eval_at);

CREATE SEQUENCE incident_seq;
CREATE TABLE incidents (
    id TEXT PRIMARY KEY,
    primary_metric TEXT NOT NULL,
    scope_key TEXT NOT NULL,
    state TEXT NOT NULL,
    updated_at TIMESTAMPTZ,
    payload JSONB NOT NULL
);
CREATE INDEX incidents_active_idx ON incidents (primary_metric, scope_key)
    WHERE state IN ('OPEN', 'DETECTED', 'ACKNOWLEDGED');

CREATE SEQUENCE outbox_seq;
CREATE TABLE outbox (
    id TEXT PRIMARY KEY,
    event_key TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL,
    delivered_at TIMESTAMPTZ,
    dead_lettered_at TIMESTAMPTZ,
    attempt_count INTEGER NOT NULL DEFAULT 0,
    last_attempt_at TIMESTAMPTZ,
    last_error TEXT,
    next_attempt_at TIMESTAMPTZ,
    claim_token TEXT,
    claimed_until TIMESTAMPTZ,
    payload JSONB NOT NULL
);
CREATE UNIQUE INDEX outbox_event_key_uq ON outbox (event_key) WHERE event_key <> '';
CREATE INDEX outbox_pending_idx ON outbox (created_at)
    WHERE delivered_at IS NULL AND dead_lettered_at IS NULL;
