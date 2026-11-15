CREATE TABLE shelley_meow_role_assignments (
    season_id BIGINT NOT NULL REFERENCES shelley_meow_seasons(id),
    user_id BIGINT NOT NULL,
    desired_rank TEXT,
    applied_role_id BIGINT,
    retry_at TIMESTAMPTZ NOT NULL DEFAULT '-infinity',
    PRIMARY KEY (season_id, user_id)
);

INSERT INTO shelley_meow_role_assignments (season_id, user_id, applied_role_id)
SELECT progress.season_id, progress.user_id, progress.applied_role_id
FROM shelley_meow_progress progress
JOIN shelley_meow_seasons season ON season.id = progress.season_id
WHERE season.state = 'active' AND progress.applied_role_id IS NOT NULL;
