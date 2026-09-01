-- policy_db_description.sql
--
-- The database-level COMMENT for policy_db, exposed to consumers via
-- pg_catalog.shobj_description() (the MCP server's list_databases reads it).
--
-- Lives here because the object it targets does: this repo owns policy_db's
-- DDL, so it also owns the database's description. Not a grant, but it sits
-- in sql/ because it shares the folder's lifecycle: database-scoped,
-- destroyed by DROP DATABASE, re-applied in the same rebuild step. (Table
-- and schema descriptions, by contrast, ride with the creating modules and
-- reapply automatically on the next ingest of each config.)
--
-- A rebuild (DROP DATABASE) drops the description with the database, so this
-- runs in the rebuild sequence alongside mcp_ro_policy_grants.sql.
--
-- Invocation (the database is taken from the connection, so -d selects it):
--   psql -d policy_db -U policy_db_maintainer \
--        -f instances/policy_db/sql/policy_db_description.sql
--
-- Preconditions:
--   * Run as the database owner (policy_db_maintainer) or a superuser —
--     COMMENT ON DATABASE requires ownership of the database.
--   * Run while connected to the target database: COMMENT ON DATABASE can
--     only target the current database.
--   * Idempotent: COMMENT ON overwrites whatever description is there, so
--     re-running after a rebuild (or to revise the text) is safe.

-- Abort on the first failed statement: a permission failure must not leave
-- the description missing or stale behind a zero exit.
\set ON_ERROR_STOP on

-- The description text below is non-ASCII (it contains an em dash). psql
-- decodes file input using client_encoding, which defaults from the OS
-- locale (WIN1252 on Windows) and would silently store mojibake in
-- pg_shdescription — the exact string the MCP server's list_databases
-- surfaces to clients. Pin the encoding so the file is read as written.
\encoding utf8

-- COMMENT ON DATABASE only ever applies to the connected database, so the
-- name is read from the connection rather than passed in: a -v variable here
-- could hold nothing but the value of -d, and any other value would only
-- raise `database "x" is not the current database`.
select current_database() as database \gset

-- The database this script is FOR. The variable exists so the script can be
-- verified against a throwaway database without touching policy_db; a bare
-- invocation gets the default and therefore aborts if pointed elsewhere.
\if :{?expect_database}
\else
    \set expect_database policy_db
\endif

-- Assert the connection before writing the description. COMMENT ON DATABASE
-- always targets the CONNECTED database, so a mistyped -d silently relabels a
-- different database with policy_db's description — and because the MCP
-- server's list_databases reads exactly this text, the mislabelling would be
-- surfaced to clients as fact. The value travels through a GUC rather than
-- :'expect_database' because psql does not interpolate variables inside a
-- dollar-quoted body.
set grants.expect_database = :'expect_database';
do $$
begin
    if current_database() <> current_setting('grants.expect_database') then
        raise exception
            'This script describes database %, but the connection is to %. '
            'Re-run with -d %, or pass -v expect_database=% deliberately.',
            current_setting('grants.expect_database'), current_database(),
            current_setting('grants.expect_database'), current_database();
    end if;
end
$$;

comment on database :"database" is
    'Healthcare policy and regulatory reference data — CMS Internet-Only Manuals '
    '(cms_iom), QPP cost measure codes (qpp_cm), and select United States Code '
    'titles (usc)';
