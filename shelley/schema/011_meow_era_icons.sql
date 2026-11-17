CREATE TABLE shelley_meow_era_icons (
    guild_id BIGINT NOT NULL REFERENCES shelley_meow_guilds(guild_id),
    era INTEGER NOT NULL CHECK (era BETWEEN 1 AND 3999),
    emoji TEXT NOT NULL CHECK (char_length(emoji) BETWEEN 1 AND 64),
    PRIMARY KEY (guild_id, era)
);
