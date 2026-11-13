-- Runs once, only against a fresh (empty) postgres data volume.
-- POSTGRES_DB (coirei_mvp) is created by the image itself; this adds the
-- GTM crawler's separate database alongside it, in the same Postgres server.
CREATE DATABASE coirei_intelligence;
