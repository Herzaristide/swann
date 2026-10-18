-- migrate:up
-- =============================================================================
-- Swann — extensions & fonctions utilitaires
-- =============================================================================

-- gen_random_uuid()
CREATE EXTENSION IF NOT EXISTS pgcrypto;

-- -----------------------------------------------------------------------------
-- Fonction utilitaire : maintien de updated_at
-- -----------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION set_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- migrate:down
-- Schéma initial : pas de retour arrière (repartir d'une base vide).
