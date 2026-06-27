CREATE TABLE IF NOT EXISTS shelley_state (
    guild_id BIGINT NOT NULL,
    key TEXT NOT NULL,
    value JSONB NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (guild_id, key)
);

CREATE TABLE IF NOT EXISTS shelley_star_forwards (
    guild_id BIGINT NOT NULL,
    source_channel_id BIGINT NOT NULL,
    source_message_id BIGINT NOT NULL,
    target_channel_id BIGINT NOT NULL,
    forwarded_message_id BIGINT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (guild_id, source_channel_id, source_message_id)
);

CREATE TABLE IF NOT EXISTS shelley_points_users (
    guild_id BIGINT NOT NULL,
    user_id BIGINT NOT NULL,
    text_points BIGINT NOT NULL DEFAULT 0,
    voice_points BIGINT NOT NULL DEFAULT 0,
    last_text_award_at DOUBLE PRECISION NOT NULL DEFAULT 0,
    last_voice_award_at DOUBLE PRECISION NOT NULL DEFAULT 0,
    last_text_channel_id BIGINT,
    last_text_message_id BIGINT,
    last_name TEXT,
    last_display_name TEXT,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (guild_id, user_id)
);

CREATE TABLE IF NOT EXISTS shelley_text_channel_cursors (
    guild_id BIGINT NOT NULL,
    channel_id BIGINT NOT NULL,
    message_id BIGINT NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (guild_id, channel_id)
);

CREATE TABLE IF NOT EXISTS shelley_recovery_controls (
    id BIGSERIAL PRIMARY KEY,
    guild_id BIGINT NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL,
    created_at_unix BIGINT NOT NULL,
    button_id TEXT,
    button_label TEXT,
    target TEXT,
    action TEXT,
    command_key TEXT,
    status TEXT,
    returncode INTEGER,
    error TEXT,
    user_id BIGINT,
    user_name TEXT,
    user_display_name TEXT,
    source_hash TEXT,
    payload JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS idx_shelley_points_text_rank ON shelley_points_users (guild_id, text_points DESC, user_id);
CREATE INDEX IF NOT EXISTS idx_shelley_points_voice_rank ON shelley_points_users (guild_id, voice_points DESC, user_id);
CREATE INDEX IF NOT EXISTS idx_shelley_recovery_created ON shelley_recovery_controls (created_at_unix);
CREATE UNIQUE INDEX IF NOT EXISTS idx_shelley_recovery_source_hash ON shelley_recovery_controls (source_hash) WHERE source_hash IS NOT NULL;
