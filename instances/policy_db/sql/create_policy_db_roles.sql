-- create_policy_db_roles.sql
--
-- Instance provisioning, step 1 of 2: creates the policy_db_maintainer
-- role and (optionally, when its password is supplied) the mcp_ro_policy
-- serving role. create_policy_db.sql is step 2 — split from this file
-- because roles are cluster-level and created once, while the database
-- can be created, dropped, and recreated (a dev database included) with
-- no credential in hand.
--
-- Run manually. Idempotent: each role is created only when absent (CREATE
-- ROLE has no IF NOT EXISTS), so a re-run never fails or resets a
-- password. Passwords are psql variables so no real credential is ever
-- committed; record the maintainer's in instances/policy_db/.env (the
-- serving credential belongs to the MCP deployment):
--   psql -d postgres -U <provisioning account> \
--        -v maintainer_password='<password>' \
--        -f instances/policy_db/sql/create_policy_db_roles.sql
--
-- Add -v serving_password='<password>' to create mcp_ro_policy in the
-- same run; without it the script only reports whether the role exists
-- (mcp_ro_policy_grants.sql fails later if it does not).
--
-- The role names are psql variables too (-v role_name=... and
-- -v serving_role=...), so the script can be proved against throwaway
-- roles without touching the real ones.
--
-- Privilege model
--   invoking account       CREATEROLE or SUPERUSER, asserted below only
--                          when a role actually needs creating.
--   policy_db_maintainer   LOGIN only -- no attributes, no memberships.
--                          Owns policy_db and everything in it (once
--                          create_policy_db.sql has run); ownership is
--                          its entire power.
--   mcp_ro_policy          LOGIN only -- no attributes, no memberships.
--                          Created here with NO grants: everything it can
--                          reach is granted (and asserted) solely by
--                          mcp_ro_policy_grants.sql.

-- Abort on the first failed statement: a half-provisioned role must not
-- hide behind a zero exit.
\set ON_ERROR_STOP on

-- Require the maintainer password up front so a bare invocation fails
-- here, with a clear message, rather than at CREATE ROLE.
\if :{?maintainer_password}
\else
    \echo 'ERROR: pass the maintainer password via -v maintainer_password=...'
    \quit 1
\endif

\if :{?role_name}
\else
    \set role_name policy_db_maintainer
\endif
\if :{?serving_role}
\else
    \set serving_role mcp_ro_policy
\endif

-- The names travel into the precondition block through GUCs because psql
-- does not interpolate variables inside a dollar-quoted body.
set provisioning.role_name = :'role_name';
set provisioning.serving_role = :'serving_role';
\if :{?serving_password}
    set provisioning.create_serving = 'true';
\else
    set provisioning.create_serving = 'false';
\endif

-- --- precondition: the account can create what this run will create ----
-- CREATE ROLE is attribute-gated, and the attribute is read from
-- current_user's own pg_roles row: role attributes are NOT inherited
-- through membership. Asserted only when a role is absent -- a re-run
-- against existing roles executes no DDL and legitimately requires
-- nothing.
do $$
declare
    target_role text := current_setting('provisioning.role_name');
    serving_role text := current_setting('provisioning.serving_role');
    will_create_serving boolean :=
        current_setting('provisioning.create_serving')::boolean
        and not exists (
            select 1 from pg_roles
             where rolname = current_setting('provisioning.serving_role')
        );
    role_exists boolean := exists (
        select 1 from pg_roles where rolname = current_setting('provisioning.role_name')
    );
    may_createrole boolean;
begin
    select rolsuper or rolcreaterole
      into may_createrole
      from pg_roles
     where rolname = current_user;

    if (not role_exists or will_create_serving) and not may_createrole then
        raise exception
            'Role(s) % must be created, but the invoking account % holds neither '
            'SUPERUSER nor CREATEROLE.',
            concat_ws(', ',
                case when not role_exists then target_role end,
                case when will_create_serving then serving_role end),
            current_user
        using hint = 'Re-run as an account that can create roles.';
    end if;
end
$$;

-- The maintainer is LOGIN-only: no attributes, no memberships, no grants
-- anywhere. Created only when absent (CREATE ROLE has no IF NOT EXISTS)
-- so a re-run never fails or resets the password.
--
-- quote_literal() around the password is REQUIRED: psql's :'var' form
-- produces a quoted literal in THIS outer SELECT, but the value that
-- reaches the concatenation is the bare password, so \gexec would receive
-- it unquoted -- a syntax error for every password. quote_literal() puts
-- the quotes into the GENERATED statement and correctly escapes an
-- embedded single quote; quote_ident() is the same fix for the
-- identifier position. Same reasoning as create_ingestion_test_role.sql.
select 'create role ' || quote_ident(:'role_name')
       || ' login password ' || quote_literal(:'maintainer_password')
       || ' nosuperuser nocreatedb nocreaterole'
where not exists (
    select 1 from pg_roles where rolname = :'role_name'
) \gexec

-- The serving role, when its password was supplied: LOGIN-only, created
-- with no grants at all. Its entire reachable surface is granted -- and
-- its emptiness of attributes and memberships asserted -- by
-- mcp_ro_policy_grants.sql, which is the authority on what it can do.
\if :{?serving_password}
select 'create role ' || quote_ident(:'serving_role')
       || ' login password ' || quote_literal(:'serving_password')
       || ' nosuperuser nocreatedb nocreaterole'
where not exists (
    select 1 from pg_roles where rolname = :'serving_role'
) \gexec
\else
-- Report rather than fail: creating the serving role here is optional,
-- and mcp_ro_policy_grants.sql fails with its own message if it is missing.
select exists (
    select 1 from pg_roles where rolname = :'serving_role'
) as serving_exists \gset
\if :serving_exists
\else
    \echo 'NOTE: serving role does not exist and no -v serving_password was given;'
    \echo '      mcp_ro_policy_grants.sql will fail until it is created.'
\endif
\endif
