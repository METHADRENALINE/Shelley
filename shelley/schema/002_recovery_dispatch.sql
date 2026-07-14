ALTER TABLE shelley_recovery_controls
ADD COLUMN IF NOT EXISTS dispatch_type TEXT;

UPDATE shelley_recovery_controls
SET dispatch_type = CASE
    WHEN returncode IS NOT NULL OR status IN ('ok', 'failed') THEN 'ssh_dispatched'
    ELSE 'not_dispatched'
END
WHERE dispatch_type IS NULL;

ALTER TABLE shelley_recovery_controls
ALTER COLUMN dispatch_type SET DEFAULT 'not_dispatched';

ALTER TABLE shelley_recovery_controls
ALTER COLUMN dispatch_type SET NOT NULL;

ALTER TABLE shelley_recovery_controls
DROP CONSTRAINT IF EXISTS shelley_recovery_dispatch_type_check;

ALTER TABLE shelley_recovery_controls
ADD CONSTRAINT shelley_recovery_dispatch_type_check
CHECK (dispatch_type IN ('ssh_dispatched', 'not_dispatched'));
