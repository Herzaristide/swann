-- migrate:up
-- =============================================================================
-- Console d'administration (docs/console.md)
-- -----------------------------------------------------------------------------
-- 1. File de tâches `jobs` prise par le worker (`swann worker`) avec
--    SELECT … FOR UPDATE SKIP LOCKED. Chaque insertion ou changement de statut
--    / d'avancement est notifié sur le canal `jobs` (le worker se réveille,
--    l'API relaie en SSE).
-- 2. `ingestions` : rattachement à la tâche et étape (extract, index, align).
-- 3. Qualité des éditions : mesures, signaux, score ; signaux acceptés.
-- 4. Corbeille : `deleted_at` sur les œuvres et les éditions. Une édition à la
--    corbeille n'est plus servie par l'API ni présente dans Qdrant ; elle est
--    purgée définitivement après un délai (worker).
-- 5. Cache des réponses Wikidata (classement des dépôts).
-- 6. `corpus_events.actor` : `sub` Keycloak de l'auteur d'une modification
--    ('cli' pour la ligne de commande, NULL pour l'ingestion automatique).
-- =============================================================================

-- ------------------------------------------------------------------ 1. jobs
CREATE TYPE job_kind AS ENUM (
    'ingest',          -- dépôt : identify → extract → quality → index → align
    'reprocess',       -- structure modifiée : quality → index → align
    'align',           -- réalignement d'une œuvre
    'sync_payload',    -- fiche → payload Qdrant
    'trash_edition',   -- mise à la corbeille : points Qdrant retirés
    'restore_edition', -- sortie de corbeille : réindexation, réalignement
    'purge',           -- suppression définitive (Postgres, Qdrant, MinIO)
    'import_books',    -- import de books/ (équivalent de swann extract)
    'quality',         -- recalcul de la qualité (une édition ou toutes)
    'cli'              -- exécution d'une commande swann, pour la supervision
);

CREATE TYPE job_status AS ENUM (
    'queued', 'running', 'needs_review', 'succeeded', 'failed', 'cancelled'
);

CREATE TABLE jobs (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    kind            job_kind NOT NULL,
    status          job_status NOT NULL DEFAULT 'queued',
    -- Libellé lisible ("Dépôt de fr.epub", "swann extract")
    title           TEXT NOT NULL,
    params          JSONB NOT NULL DEFAULT '{}',
    -- {step, done, total, message, tokens_per_s}
    progress        JSONB NOT NULL DEFAULT '{}',
    -- Résultat (edition_id, rapport d'identification, candidats…)
    result          JSONB,
    error           TEXT,
    parent_id       UUID REFERENCES jobs (id) ON DELETE CASCADE,
    work_id         UUID REFERENCES works (id) ON DELETE SET NULL,
    edition_id      UUID REFERENCES editions (id) ON DELETE SET NULL,
    created_by_sub  TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    started_at      TIMESTAMPTZ,
    heartbeat_at    TIMESTAMPTZ,
    finished_at     TIMESTAMPTZ,
    attempts        SMALLINT NOT NULL DEFAULT 0,
    -- Demande d'annulation, lue par le worker entre deux étapes
    cancel_requested BOOLEAN NOT NULL DEFAULT false
);

CREATE INDEX jobs_queue_idx ON jobs (created_at) WHERE status = 'queued';
CREATE INDEX jobs_status_idx ON jobs (status, created_at DESC);
CREATE INDEX jobs_parent_idx ON jobs (parent_id);
CREATE INDEX jobs_edition_idx ON jobs (edition_id);
CREATE INDEX jobs_work_idx ON jobs (work_id);
CREATE INDEX jobs_created_idx ON jobs (created_at DESC);

CREATE FUNCTION jobs_notify() RETURNS trigger AS $$
BEGIN
    IF TG_OP = 'INSERT'
       OR NEW.status IS DISTINCT FROM OLD.status
       OR NEW.progress IS DISTINCT FROM OLD.progress THEN
        PERFORM pg_notify('jobs', json_build_object(
            'id', NEW.id, 'kind', NEW.kind, 'status', NEW.status,
            'parent_id', NEW.parent_id)::TEXT);
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER jobs_notify
    AFTER INSERT OR UPDATE ON jobs
    FOR EACH ROW EXECUTE FUNCTION jobs_notify();

-- ------------------------------------------------------------ 2. ingestions
ALTER TABLE ingestions ADD COLUMN job_id UUID REFERENCES jobs (id) ON DELETE SET NULL;
ALTER TABLE ingestions ADD COLUMN stage TEXT;
ALTER TABLE ingestions ADD CONSTRAINT ingestions_stage_check
    CHECK (stage IS NULL OR stage IN ('extract', 'index', 'align'));
CREATE INDEX ingestions_job_idx ON ingestions (job_id);
CREATE INDEX ingestions_started_idx ON ingestions (started_at DESC);

-- --------------------------------------------------------------- 3. qualité
-- parse_* : connu seulement à la lecture de l'EPUB (renseigné par extract).
-- metrics : calculé depuis la base (étape quality, `swann quality`).
-- signals : codes des problèmes détectés (voir swann_ingest.quality).
CREATE TABLE edition_quality (
    edition_id         UUID PRIMARY KEY REFERENCES editions (id) ON DELETE CASCADE,
    structure_method   TEXT,
    parse_warnings     JSONB NOT NULL DEFAULT '[]',
    detected_language  TEXT,
    opf_metadata       JSONB,
    parsed_at          TIMESTAMPTZ,
    metrics            JSONB NOT NULL DEFAULT '{}',
    signals            TEXT[] NOT NULL DEFAULT '{}',
    score              SMALLINT,
    computed_at        TIMESTAMPTZ,
    CONSTRAINT edition_quality_score_range CHECK (score IS NULL OR score BETWEEN 0 AND 100)
);

CREATE INDEX edition_quality_signals_idx ON edition_quality USING gin (signals);
CREATE INDEX edition_quality_score_idx ON edition_quality (score);

-- Signal examiné et accepté par un admin : ne remonte plus dans les listes.
CREATE TABLE quality_acks (
    edition_id  UUID NOT NULL REFERENCES editions (id) ON DELETE CASCADE,
    signal      TEXT NOT NULL,
    acked_by_sub TEXT,
    comment     TEXT,
    acked_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (edition_id, signal)
);

-- ------------------------------------------------------------- 4. corbeille
ALTER TABLE works ADD COLUMN deleted_at TIMESTAMPTZ;
ALTER TABLE works ADD COLUMN deleted_by_sub TEXT;
ALTER TABLE editions ADD COLUMN deleted_at TIMESTAMPTZ;
ALTER TABLE editions ADD COLUMN deleted_by_sub TEXT;
CREATE INDEX works_deleted_idx ON works (deleted_at) WHERE deleted_at IS NOT NULL;
CREATE INDEX editions_deleted_idx ON editions (deleted_at) WHERE deleted_at IS NOT NULL;

-- Rapport d'identification d'une édition déposée (comment elle a été classée).
ALTER TABLE editions ADD COLUMN identification JSONB;

-- -------------------------------------------------------------- 5. wikidata
CREATE TABLE wikidata_cache (
    key         TEXT PRIMARY KEY,
    value       JSONB NOT NULL,
    fetched_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------- 6. acteur
ALTER TABLE corpus_events ADD COLUMN actor TEXT;
CREATE INDEX corpus_events_at_idx ON corpus_events (at DESC);

-- Variante avec auteur (les appels à 3 ou 4 arguments restent valables).
CREATE FUNCTION corpus_emit(p_type TEXT, p_work_id UUID, p_edition_id UUID, p_data JSONB, p_actor TEXT)
RETURNS BIGINT AS $$
DECLARE
    event_id BIGINT;
BEGIN
    PERFORM pg_advisory_xact_lock(hashtext('corpus_events'));
    INSERT INTO corpus_events (type, work_id, edition_id, data, actor)
    VALUES (p_type, p_work_id, p_edition_id, coalesce(p_data, '{}'), p_actor)
    RETURNING id INTO event_id;
    PERFORM pg_notify('corpus_events', event_id::TEXT);
    RETURN event_id;
END;
$$ LANGUAGE plpgsql;

-- migrate:down
DROP FUNCTION corpus_emit(TEXT, UUID, UUID, JSONB, TEXT);
DROP INDEX corpus_events_at_idx;
ALTER TABLE corpus_events DROP COLUMN actor;
DROP TABLE wikidata_cache;
ALTER TABLE editions DROP COLUMN identification;
DROP INDEX editions_deleted_idx;
DROP INDEX works_deleted_idx;
ALTER TABLE editions DROP COLUMN deleted_by_sub;
ALTER TABLE editions DROP COLUMN deleted_at;
ALTER TABLE works DROP COLUMN deleted_by_sub;
ALTER TABLE works DROP COLUMN deleted_at;
DROP TABLE quality_acks;
DROP TABLE edition_quality;
DROP INDEX ingestions_started_idx;
DROP INDEX ingestions_job_idx;
ALTER TABLE ingestions DROP CONSTRAINT ingestions_stage_check;
ALTER TABLE ingestions DROP COLUMN stage;
ALTER TABLE ingestions DROP COLUMN job_id;
DROP TRIGGER jobs_notify ON jobs;
DROP FUNCTION jobs_notify();
DROP TABLE jobs;
DROP TYPE job_status;
DROP TYPE job_kind;
