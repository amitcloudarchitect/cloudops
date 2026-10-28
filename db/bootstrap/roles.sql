-- Server-level roles the migrations need, created once per server by its
-- administrator, before the first `alembic upgrade`. Idempotent: safe to rerun.
--
--   psql "<admin connection>" -v migrator=<migration role> -f db/bootstrap/roles.sql
--
-- Locally, infra/local/postgres/init.sh runs this file as the superuser. In
-- Azure, the Entra administrator of the Flexible Server runs it (README,
-- "Azure database bootstrap"). Roles are cluster-wide, so a migration cannot
-- safely create them for itself: it could not later grant a role it did not
-- create, and must never be able to bypass RLS.
--
--   cloudops_app, cloudops_ingest     group roles the migrations grant privileges
--                                     to; login identities are made members of them
--   cloudops_resolver,                own the access-control functions
--   cloudops_access_admin             (migration 0004); reachable only through them
--
-- All four are NOLOGIN, NOSUPERUSER, NOBYPASSRLS. The migration role becomes a
-- member of the two function owners WITH INHERIT FALSE: it may hand ownership to
-- them, but never acts with their rights.

\set ON_ERROR_STOP on

BEGIN;

-- psql variables are not substituted inside a DO body; pass the name through a
-- transaction-local setting instead.
\o /dev/null
SELECT set_config('cloudops_bootstrap.migrator', :'migrator', true);
\o

DO $$
DECLARE
    migrator text := current_setting('cloudops_bootstrap.migrator');
    r text;
BEGIN
    IF migrator = '' OR NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = migrator) THEN
        RAISE EXCEPTION 'migration role "%" does not exist; create it first', migrator;
    END IF;
    FOREACH r IN ARRAY ARRAY['cloudops_app', 'cloudops_ingest',
                             'cloudops_resolver', 'cloudops_access_admin'] LOOP
        IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = r) THEN
            EXECUTE format('CREATE ROLE %I NOLOGIN NOSUPERUSER NOBYPASSRLS NOINHERIT', r);
            RAISE NOTICE 'created %', r;
        ELSIF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = r
                      AND (rolcanlogin OR rolsuper OR rolbypassrls)) THEN
            -- Never "fix" an existing role silently: someone made it this way.
            RAISE EXCEPTION 'role % exists but can log in, is superuser or bypasses RLS', r;
        END IF;
    END LOOP;
    EXECUTE format(
        'GRANT cloudops_resolver, cloudops_access_admin TO %I WITH INHERIT FALSE', migrator);
END $$;

COMMIT;
