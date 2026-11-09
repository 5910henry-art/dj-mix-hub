BEGIN;

CREATE TABLE djmix_hub.mix_rights_declarations (
    id SERIAL PRIMARY KEY,
    mix_id INTEGER NOT NULL UNIQUE
        REFERENCES djmix_hub.mixes(id) ON DELETE CASCADE,
    dj_id INTEGER NOT NULL
        REFERENCES djmix_hub.djs(id) ON DELETE CASCADE,
    confirmed_at TIMESTAMP WITHOUT TIME ZONE NOT NULL,
    declaration_version VARCHAR(30) NOT NULL,
    declaration_text TEXT NOT NULL
);

CREATE INDEX ix_mix_rights_declarations_dj_id
    ON djmix_hub.mix_rights_declarations(dj_id);

COMMIT;
