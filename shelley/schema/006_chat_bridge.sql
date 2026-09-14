CREATE TABLE IF NOT EXISTS shelley_chat_bridge (
    sequence BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    route TEXT NOT NULL,
    node TEXT NOT NULL,
    direction TEXT NOT NULL CHECK (direction IN ('discord', 'minecraft')),
    event_id TEXT NOT NULL,
    username TEXT NOT NULL DEFAULT '',
    content TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at TIMESTAMPTZ NOT NULL,
    delivered_at TIMESTAMPTZ,
    discord_message_id BIGINT,
    UNIQUE (route, node, direction, event_id)
);
CREATE INDEX IF NOT EXISTS shelley_chat_bridge_pending
    ON shelley_chat_bridge (route, node, direction, sequence)
    WHERE delivered_at IS NULL;
