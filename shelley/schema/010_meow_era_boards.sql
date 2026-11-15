ALTER TABLE shelley_meow_seasons ADD COLUMN winner_username TEXT;

CREATE UNIQUE INDEX shelley_meow_era_stage_unique ON shelley_meow_seasons
    (guild_id, (design #>> '{era_slot,era}'), (design #>> '{era_slot,stage}'))
    WHERE state IN ('active', 'awarding', 'finished') AND design ->> 'era_slot' IS NOT NULL;

CREATE TABLE shelley_meow_era_boards (
    guild_id BIGINT NOT NULL REFERENCES shelley_meow_guilds(guild_id),
    era INTEGER NOT NULL CHECK (era BETWEEN 1 AND 3999),
    channel_id BIGINT NOT NULL,
    message_id BIGINT,
    signature TEXT,
    PRIMARY KEY (guild_id, era),
    UNIQUE (guild_id, message_id)
);

INSERT INTO shelley_meow_era_boards (guild_id, era, channel_id, message_id)
SELECT guild_id, 1, era_archive_channel_id, era_archive_message_id
FROM shelley_meow_guilds
WHERE era_archive_channel_id IS NOT NULL AND era_archive_message_id IS NOT NULL;
