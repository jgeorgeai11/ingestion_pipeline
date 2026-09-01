-- create_policy_db.sql
--
-- Instance provisioning, step 2 of 2: creates the policy_db database
-- owned by the maintainer role create_policy_db_roles.sql created, and installs the
-- ltree and vector extensions into it. The companion scripts run after
-- every rebuild: mcp_ro_policy_grants.sql (the serving role's entire grant
-- surface) and policy_db_description.sql.
--
-- Run manually, after create_policy_db_roles.sql. No passwords: this script touches
-- no credential, so recreating a dropped database -- or provisioning a
-- second one under the same roles, e.g. a dev database -- needs nothing
-- secret in hand:
--   psql -d postgres -U <provisioning account> \
--        -f instances/policy_db/sql/create_policy_db.sql
--
-- Idempotent: the database is created only when absent and the extension
-- installs are IF NOT EXISTS. The object names are psql variables, so the
-- script can be proved end to end against a throwaway database -- and a
-- dev database is the same move deliberately:
--   psql -d postgres -U <provisioning account> \
--        -v db_name=policy_db_dev \
--        -f instances/policy_db/sql/create_policy_db.sql
--
-- Privilege model
--   invoking account       Must be able to (a) CREATE DATABASE owned by
--                          the maintainer role -- which on PostgreSQL 16+
--                          requires SET ROLE on the owner; the remedy is
--                          a self-grant the precondition below prints
--                          verbatim and deliberately does NOT perform --
--                          and (b) install the extensions: a superuser,
--                          or a cluster whose template1 was seeded. Each
--                          is asserted below, only when this run actually
--                          needs it.
--   policy_db              owned by policy_db_maintainer; ownership is
--                          the maintainer's entire power, and the
--                          engine's DDL creates the schemas and tables
--                          inside it at ingest.
--
-- Extension contract: the engine VERIFIES extensions (ingpipe_lib.db.
-- require_extensions) and never creates them -- installing one is a
-- provisioning act requiring privileges an ingest run must not hold.

-- Abort on the first failed statement: a half-provisioned database must
-- not hide behind a zero exit.
\set ON_ERROR_STOP on

-- Defaults for the object names, applied only when not supplied via -v.
\if :{?role_name}
\else
    \set role_name policy_db_maintainer
\endif
\if :{?db_name}
\else
    \set db_name policy_db
\endif

-- The names travel into the precondition blocks through GUCs because psql
-- does not interpolate variables inside a dollar-quoted body.
set provisioning.role_name = :'role_name';
set provisioning.db_name = :'db_name';

-- --- precondition: the owner role exists, and the account can proceed --
-- The role is this script's input, not its product: it must already exist
-- (create_policy_db_roles.sql creates it). CREATE DATABASE is attribute-gated, and
-- the attribute is read from current_user's own pg_roles row: role
-- attributes are NOT inherited through membership. Asserted only when the
-- database is absent -- a re-run against an existing database executes no
-- DDL and legitimately requires nothing.
do $$
declare
    target_role text := current_setting('provisioning.role_name');
    target_db text := current_setting('provisioning.db_name');
    db_exists boolean := exists (
        select 1 from pg_database where datname = current_setting('provisioning.db_name')
    );
    may_createdb boolean;
begin
    if db_exists then
        return;  -- nothing below creates anything; no ability is needed
    end if;

    if not exists (select 1 from pg_roles where rolname = target_role) then
        raise exception
            'Role % does not exist, so no database can be owned by it.',
            target_role
        using hint = 'Run create_policy_db_roles.sql first.';
    end if;

    select rolsuper or rolcreatedb
      into may_createdb
      from pg_roles
     where rolname = current_user;

    if not may_createdb then
        raise exception
            'Database % does not exist and must be created, but the invoking account % '
            'holds neither SUPERUSER nor CREATEDB.',
            target_db, current_user
        using hint = 'Re-run as an account that can create databases.';
    end if;
end
$$;

-- --- precondition: SET ROLE on the owner ------------------------------
-- `create database ... owner <role>` requires the creator to be able to
-- SET ROLE to that role (PostgreSQL 16+; plain membership before 16, and
-- 'SET' is not a recognised mode there at all -- the call raises rather
-- than returning false). Checked here so a run that cannot succeed aborts
-- before touching anything. Creating the role is not enough to pass: a
-- CREATEROLE account receives admin_option on the roles it creates, but
-- not the set_option this requires.
do $$
declare
    target_role text := current_setting('provisioning.role_name');
    on_pg16 boolean := current_setting('server_version_num')::int >= 160000;
begin
    if exists (select 1 from pg_database where datname = current_setting('provisioning.db_name'))
    then
        return;  -- the database already exists; no ownership is being assigned
    end if;

    if not pg_has_role(current_user, target_role,
                       case when on_pg16 then 'SET' else 'MEMBER' end) then
        raise exception
            'Account % cannot SET ROLE to %, so it cannot create a database owned by it.',
            current_user, target_role
        using hint = format('Run: grant %I to current_user%s;', target_role,
                            case when on_pg16 then ' with set true' else '' end);
    end if;
end
$$;

-- Ownership implies CREATE on the database, so the maintainer needs no
-- further grants for the engine's DDL. (CREATE DATABASE cannot run inside
-- a transaction block, so this script is not wrapped in begin/commit;
-- ON_ERROR_STOP still prevents it running after a failed precondition.)
-- Created only when absent so a re-run is a no-op here.
select 'create database ' || quote_ident(:'db_name')
       || ' owner ' || quote_ident(:'role_name')
where not exists (
    select 1 from pg_database where datname = :'db_name'
) \gexec

-- Install the extensions. IF NOT EXISTS keeps the re-run idempotent. This
-- must run inside the target database (extensions are per-database), and
-- the name is parameterized: left literal, a scratch verification run
-- would create its throwaway database and then install into the REAL one,
-- breaking the very thing it was proving.
\connect :"db_name"

-- --- precondition: the extensions can actually be installed ------------
-- Without this the run ends at the raw `permission denied to create
-- extension "vector"` -- accurate, but naming no way forward. Only
-- extensions that are BOTH missing and out of reach for this account are
-- reported, so a database that already has them (e.g. inherited from a
-- seeded template1) passes regardless of who is connected.
do $$
declare
    required text[] := array['ltree', 'vector'];
    unavailable text[];
    blocked text[];
    is_super boolean := (select rolsuper from pg_roles where rolname = current_user);
    may_create_here boolean := has_database_privilege(current_database(), 'CREATE');
begin
    -- Missing from the database AND not offered by the server at all: the
    -- extension's files are not installed, which no privilege fixes.
    select array_agg(req.name order by req.name)
      into unavailable
      from unnest(required) as req(name)
      left join pg_extension installed on installed.extname = req.name
      left join pg_available_extensions avail on avail.name = req.name
     where installed.extname is null
       and avail.name is null;

    if unavailable is not null then
        raise exception
            'Extension(s) % are not available on this server, so database % cannot be '
            'provisioned here.',
            array_to_string(unavailable, ', '), current_database()
        using hint = 'Install the extension packages on the server, then re-run.';
    end if;

    -- Missing, available, but out of reach for this account. A trusted
    -- extension needs only CREATE on the database; an untrusted one
    -- (vector) needs a superuser.
    select array_agg(req.name order by req.name)
      into blocked
      from unnest(required) as req(name)
      left join pg_extension installed on installed.extname = req.name
      join pg_available_extensions avail on avail.name = req.name
      join pg_available_extension_versions ver
        on ver.name = avail.name and ver.version = avail.default_version
     where installed.extname is null
       and not (is_super or (ver.trusted and may_create_here));

    if blocked is not null then
        raise exception
            'Extension(s) % are missing from database % and account % may not create them.',
            array_to_string(blocked, ', '), current_database(), current_user
        using hint =
            'Either run this script as a role permitted to install them (vector is not a '
            'trusted extension, so in practice a superuser), or seed the cluster''s '
            'template1 with them. Seeding template1 affects only databases created '
            'AFTERWARDS, so an existing database still needs the privileged install.';
    end if;
end
$$;

create extension if not exists ltree;
create extension if not exists vector;
