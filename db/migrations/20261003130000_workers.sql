-- migrate:up
-- =============================================================================
-- Workers d'ingestion (`swann worker`) : présence et matériel, pour la
-- supervision. Chaque worker met à jour `seen_at` toutes les 10 s, même sans
-- tâche ; au-delà de 60 s sans nouvelle, la console le montre arrêté.
-- =============================================================================
CREATE TABLE workers (
    id              TEXT PRIMARY KEY,            -- hôte:pid
    hostname        TEXT NOT NULL,
    device          TEXT,                        -- cuda / cpu
    gpu_name        TEXT,
    models          JSONB NOT NULL DEFAULT '{}', -- modèles chargés
    current_job_id  UUID REFERENCES jobs (id) ON DELETE SET NULL,
    started_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    seen_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    stopped_at      TIMESTAMPTZ
);

-- migrate:down
DROP TABLE workers;
