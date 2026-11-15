CREATE TABLE shelley_meow_era_medals (
    guild_id BIGINT NOT NULL REFERENCES shelley_meow_guilds(guild_id),
    stage SMALLINT NOT NULL CHECK (stage BETWEEN 1 AND 6),
    winner_id BIGINT NOT NULL CHECK (winner_id > 0),
    username TEXT NOT NULL CHECK (char_length(username) BETWEEN 1 AND 32),
    role_id BIGINT NOT NULL CHECK (role_id > 0),
    medal_emoji TEXT NOT NULL CHECK (char_length(medal_emoji) BETWEEN 1 AND 64),
    season_emoji TEXT,
    recorded_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (guild_id, stage),
    UNIQUE (guild_id, role_id)
);

CREATE INDEX shelley_meow_era_medals_winner ON shelley_meow_era_medals (guild_id, winner_id);

ALTER TABLE shelley_meow_guilds
    ADD COLUMN era_archive_channel_id BIGINT,
    ADD COLUMN era_archive_message_id BIGINT,
    ADD COLUMN retired_showcase_message_id BIGINT;
