CREATE TABLE IF NOT EXISTS shelley_trademarks (
    id TEXT PRIMARY KEY,
    guild_id BIGINT NOT NULL,
    display_name TEXT NOT NULL,
    normalized_name TEXT NOT NULL,
    owner_id BIGINT,
    owner_name TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    cycle_started_at TIMESTAMPTZ,
    owner_since TIMESTAMPTZ,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT shelley_trademarks_id_format CHECK (
        id ~ '^[ABCDEFGHJKMNPQRSTVWXYZ23456789]{5}(-[ABCDEFGHJKMNPQRSTVWXYZ23456789]{5}){4}$'
    ),
    CONSTRAINT shelley_trademarks_owner_state CHECK (
        (owner_id IS NULL AND owner_name IS NULL AND cycle_started_at IS NULL AND owner_since IS NULL)
        OR
        (owner_id IS NOT NULL AND owner_name IS NOT NULL AND cycle_started_at IS NOT NULL AND owner_since IS NOT NULL)
    ),
    UNIQUE (guild_id, normalized_name)
);

CREATE TABLE IF NOT EXISTS shelley_trademark_events (
    id BIGSERIAL PRIMARY KEY,
    guild_id BIGINT NOT NULL,
    trademark_id TEXT NOT NULL REFERENCES shelley_trademarks(id),
    event_type TEXT NOT NULL,
    actor_id BIGINT NOT NULL,
    actor_name TEXT NOT NULL,
    from_user_id BIGINT,
    from_user_name TEXT,
    to_user_id BIGINT,
    to_user_name TEXT,
    related_trademark_id TEXT REFERENCES shelley_trademarks(id),
    related_trademark_name TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT shelley_trademark_events_type CHECK (
        event_type IN ('patent', 'release', 'admin_release', 'gift', 'exchange')
    )
);

CREATE TABLE IF NOT EXISTS shelley_trademark_showcase (
    guild_id BIGINT NOT NULL,
    user_id BIGINT NOT NULL,
    trademark_id TEXT NOT NULL REFERENCES shelley_trademarks(id) ON DELETE CASCADE,
    position SMALLINT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (guild_id, user_id, trademark_id),
    UNIQUE (guild_id, user_id, position),
    CONSTRAINT shelley_trademark_showcase_position CHECK (position >= 1)
);

CREATE TABLE IF NOT EXISTS shelley_trademark_patent_windows (
    guild_id BIGINT NOT NULL,
    user_id BIGINT NOT NULL,
    window_started_at TIMESTAMPTZ,
    successful_count INTEGER NOT NULL DEFAULT 0,
    last_patent_at TIMESTAMPTZ,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (guild_id, user_id),
    CONSTRAINT shelley_trademark_patent_count CHECK (successful_count >= 0)
);

CREATE TABLE IF NOT EXISTS shelley_trademark_requests (
    id UUID PRIMARY KEY,
    guild_id BIGINT NOT NULL,
    request_type TEXT NOT NULL,
    sender_id BIGINT NOT NULL,
    sender_name TEXT NOT NULL,
    recipient_id BIGINT NOT NULL,
    recipient_name TEXT NOT NULL,
    offered_trademark_id TEXT NOT NULL REFERENCES shelley_trademarks(id),
    requested_trademark_id TEXT REFERENCES shelley_trademarks(id),
    source_channel_id BIGINT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at TIMESTAMPTZ NOT NULL,
    resolved_at TIMESTAMPTZ,
    resolved_by_id BIGINT,
    resolved_by_name TEXT,
    CONSTRAINT shelley_trademark_requests_type CHECK (
        request_type IN ('exchange', 'gift')
    ),
    CONSTRAINT shelley_trademark_requests_status CHECK (
        status IN ('pending', 'accepted', 'declined', 'cancelled', 'expired', 'invalidated')
    ),
    CONSTRAINT shelley_trademark_requests_participants CHECK (
        sender_id <> recipient_id
    ),
    CONSTRAINT shelley_trademark_requests_shape CHECK (
        (request_type = 'exchange' AND requested_trademark_id IS NOT NULL)
        OR
        (request_type = 'gift' AND requested_trademark_id IS NULL)
    )
);

CREATE INDEX IF NOT EXISTS idx_shelley_trademarks_owner
ON shelley_trademarks (guild_id, owner_id, created_at, id);

CREATE INDEX IF NOT EXISTS idx_shelley_trademarks_all
ON shelley_trademarks (guild_id, created_at, id);

CREATE INDEX IF NOT EXISTS idx_shelley_trademark_events_history
ON shelley_trademark_events (guild_id, trademark_id, created_at DESC, id DESC);

CREATE INDEX IF NOT EXISTS idx_shelley_trademark_requests_incoming
ON shelley_trademark_requests (guild_id, recipient_id, status, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_shelley_trademark_requests_outgoing
ON shelley_trademark_requests (guild_id, sender_id, status, created_at DESC);

CREATE UNIQUE INDEX IF NOT EXISTS idx_shelley_trademark_requests_duplicate
ON shelley_trademark_requests (
    guild_id,
    request_type,
    sender_id,
    recipient_id,
    offered_trademark_id,
    COALESCE(requested_trademark_id, '')
)
WHERE status = 'pending';
