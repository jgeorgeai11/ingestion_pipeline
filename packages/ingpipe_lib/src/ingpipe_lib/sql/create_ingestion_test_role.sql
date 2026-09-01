-- create_ingestion_test_role.sql
--
-- Engine test infrastructure, step 1 of 2: creates the LOGIN-only role
-- that owns the dedicated test database. create_ingestion_test_db.sql is
-- step 2 — split from this file because the role is cluster-level and
-- created once, while the database can be dropped and recreated (and its
-- extensions re-run) with no credential in hand.
--
-- This file is engine-owned (deliberately NOT under an instance's sql/):
-- the test role serves the engine's own suite and must survive an
-- eventual engine/instance repo split.
--
-- Run manually. Idempotent: the role is created only when absent (CREATE
-- ROLE has no IF NOT EXISTS), so a re-run never fails or resets the
-- password. The password is a psql variable so no real credential is ever
-- committed; pass it at run time and record the same value in .env.test:
--   psql -d postgres -U <provisioning account> \
--        -v ingestion_test_password='<password>' \
--        -f packages/ingpipe_lib/src/ingpipe_lib/sql/create_ingestion_test_role.sql
--
-- The role name is a psql variable too (-v role_name=..., default
-- ingestion_test_runner), so the script can be proved against a throwaway
-- role without touching the real one.
--
-- Privilege model
--   invoking account       CREATEROLE or SUPERUSER, asserted below only
--                          when the role actually needs creating.
--   ingestion_test_runner  LOGIN only -- NOSUPERUSER NOCREATEDB
--                          NOCREATEROLE, no memberships, and no grants on
--                          any existing database. Everything it can do
--                          flows from owning the test database that
--                          create_ingestion_test_db.sql creates.

-- Abort on the first failed statement: a half-provisioned role must not
-- hide behind a zero exit.
\set ON_ERROR_STOP on

-- Require the password variable up front so a bare invocation fails here,
-- with a clear message, rather than at CREATE ROLE with a syntax error.
\if :{?ingestion_test_password}
\else
    \echo 'ERROR: pass the role password via -v ingestion_test_password=...'
    \quit 1
\endif

\if :{?role_name}
\else
    \set role_name ingestion_test_runner
\endif

-- The name travels into the precondition block through a GUC because psql
-- does not interpolate variables inside a dollar-quoted body.
set provisioning.role_name = :'role_name';

-- --- precondition: the account can create the role ---------------------
-- CREATE ROLE is attribute-gated, and the attribute is read from
-- current_user's own pg_roles row: role attributes are NOT inherited
-- through membership, so a group grant cannot supply it. Asserted only
-- when the role is absent -- a re-run against an existing role executes
-- no DDL and legitimately requires nothing.
do $$
declare
    target_role text := current_setting('provisioning.role_name');
    role_exists boolean := exists (
        select 1 from pg_roles where rolname = current_setting('provisioning.role_name')
    );
    may_createrole boolean;
begin
    select rolsuper or rolcreaterole
      into may_createrole
      from pg_roles
     where rolname = current_user;

    if not role_exists and not may_createrole then
        raise exception
            'Role % does not exist and must be created, but the invoking account % '
            'holds neither SUPERUSER nor CREATEROLE.',
            target_role, current_user
        using hint = 'Re-run as an account that can create roles.';
    end if;
end
$$;

-- The role is LOGIN-only: no CREATEDB, no CREATEROLE, no memberships, and no
-- grants on any existing database. Created only when absent (CREATE ROLE has
-- no IF NOT EXISTS) so a re-run never fails or resets the password.
--
-- quote_literal() around the password is REQUIRED and must not be simplified
-- back to a bare :'ingestion_test_password'. psql's :'var' form produces a
-- quoted literal in THIS outer SELECT, but the value that reaches the
-- concatenation is the bare password, so \gexec would receive
-- `create role r login password s6Mn... nosuperuser` -- unquoted, and a
-- syntax error for every password, which is exactly how this script failed
-- on 2026-08-25. quote_literal() puts the quotes into the GENERATED
-- statement, and correctly escapes a password containing a single quote
-- instead of turning it into a second syntax error. quote_ident() on the
-- role name is the same fix for the identifier position.
select 'create role ' || quote_ident(:'role_name')
       || ' login password ' || quote_literal(:'ingestion_test_password')
       || ' nosuperuser nocreatedb nocreaterole'
where not exists (
    select 1 from pg_roles where rolname = :'role_name'
) \gexec
