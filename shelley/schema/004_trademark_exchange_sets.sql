ALTER TABLE shelley_trademark_requests
ADD COLUMN offered_trademark_ids TEXT[];

ALTER TABLE shelley_trademark_requests
ADD COLUMN requested_trademark_ids TEXT[];

UPDATE shelley_trademark_requests
SET offered_trademark_ids = ARRAY[offered_trademark_id],
    requested_trademark_ids = CASE
        WHEN requested_trademark_id IS NULL THEN ARRAY[]::TEXT[]
        ELSE ARRAY[requested_trademark_id]
    END;

ALTER TABLE shelley_trademark_requests
ALTER COLUMN offered_trademark_ids SET NOT NULL;

ALTER TABLE shelley_trademark_requests
ALTER COLUMN requested_trademark_ids SET NOT NULL;

ALTER TABLE shelley_trademark_requests
DROP CONSTRAINT shelley_trademark_requests_shape;

ALTER TABLE shelley_trademark_requests
ADD CONSTRAINT shelley_trademark_requests_shape CHECK (
    (
        request_type = 'exchange'
        AND cardinality(offered_trademark_ids) BETWEEN 1 AND 5
        AND cardinality(requested_trademark_ids) BETWEEN 1 AND 5
        AND NOT (offered_trademark_ids && requested_trademark_ids)
    )
    OR
    (
        request_type = 'gift'
        AND cardinality(offered_trademark_ids) = 1
        AND cardinality(requested_trademark_ids) = 0
    )
);

ALTER TABLE shelley_trademark_requests
ADD CONSTRAINT shelley_trademark_requests_primary_marks CHECK (
    offered_trademark_id = offered_trademark_ids[1]
    AND requested_trademark_id IS NOT DISTINCT FROM requested_trademark_ids[1]
);

DROP INDEX idx_shelley_trademark_requests_duplicate;

CREATE UNIQUE INDEX idx_shelley_trademark_requests_duplicate
ON shelley_trademark_requests (
    guild_id,
    request_type,
    sender_id,
    recipient_id,
    offered_trademark_ids,
    requested_trademark_ids
)
WHERE status = 'pending';
