BEGIN;

ALTER TABLE djmix_hub.mix_rights_declarations
ADD COLUMN IF NOT EXISTS allow_downloads BOOLEAN NOT NULL DEFAULT FALSE;

COMMIT;
