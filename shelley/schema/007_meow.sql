CREATE TABLE shelley_meow_guilds (
    guild_id BIGINT PRIMARY KEY,
    draft JSONB NOT NULL DEFAULT '{}',
    draft_revision BIGINT NOT NULL DEFAULT 0,
    draft_season_id BIGINT,
    showcase_channel_id BIGINT,
    showcase_message_id BIGINT,
    showcase_season_id BIGINT,
    showcase_revision BIGINT NOT NULL DEFAULT 0,
    trial_channel_id BIGINT,
    trial_message_id BIGINT
);

CREATE TABLE shelley_meow_seasons (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    guild_id BIGINT NOT NULL REFERENCES shelley_meow_guilds(guild_id),
    source TEXT NOT NULL CHECK (source IN ('text', 'voice')),
    design JSONB NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('preparing', 'active', 'awarding', 'finished', 'cancelled')),
    roles JSONB NOT NULL DEFAULT '{}',
    revision BIGINT NOT NULL DEFAULT 1,
    role_revision BIGINT NOT NULL DEFAULT 0,
    draft_revision BIGINT NOT NULL,
    started_at TIMESTAMPTZ,
    closed_at TIMESTAMPTZ,
    winner_id BIGINT,
    test_user_id BIGINT,
    cleaned BOOLEAN NOT NULL DEFAULT FALSE,
    CHECK ((state IN ('awarding', 'finished')) = (winner_id IS NOT NULL)),
    CHECK ((state IN ('finished', 'cancelled')) = (closed_at IS NOT NULL)),
    CHECK (state IN ('preparing', 'cancelled') OR started_at IS NOT NULL)
);

CREATE UNIQUE INDEX shelley_meow_one_live_season ON shelley_meow_seasons (guild_id)
    WHERE state IN ('active', 'awarding');
CREATE UNIQUE INDEX shelley_meow_one_preparing_season ON shelley_meow_seasons (guild_id)
    WHERE state = 'preparing';
CREATE INDEX shelley_meow_season_history ON shelley_meow_seasons (guild_id, id DESC);
CREATE INDEX shelley_meow_cleanup ON shelley_meow_seasons (guild_id, id)
    WHERE state IN ('finished', 'cancelled') AND NOT cleaned;

CREATE TABLE shelley_meow_progress (
    season_id BIGINT NOT NULL REFERENCES shelley_meow_seasons(id),
    user_id BIGINT NOT NULL,
    points BIGINT NOT NULL CHECK (points > 0),
    desired_rank TEXT,
    applied_role_id BIGINT,
    retry_at TIMESTAMPTZ NOT NULL DEFAULT '-infinity',
    PRIMARY KEY (season_id, user_id)
);

CREATE TABLE shelley_meow_text_awards (
    season_id BIGINT NOT NULL REFERENCES shelley_meow_seasons(id),
    message_id BIGINT NOT NULL,
    PRIMARY KEY (season_id, message_id)
);
