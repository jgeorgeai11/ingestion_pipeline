-- create_ingestion_test_db.sql
--
-- Engine test infrastructure, step 2 of 2: creates the dedicated test
-- database owned by the role create_ingestion_test_role.sql created, and
-- installs the extensions the engine's DDL relies on. The suite's
-- ephemeral-schema fixtures connect as ingestion_test_runner to
-- ingestion_test (via the repo-root .env.test file), so a buggy test is
-- structurally unable to touch any real corpus database.
--
-- This file is engine-owned (deliberately NOT under an instance's sql/):
-- the test database serves the engine's own suite and must survive an
-- eventual engine/instance repo split.
--
-- Run manually, after create_ingestion_test_role.sql. No passwords: this
-- script touches no credential, so recreating a dropped database — or
-- re-running against an existing one to add a newly required extension —
-- needs nothing secret in hand. Idempotent: the database is created only
-- when absent and the extension installs are IF NOT EXISTS.
--   psql -d postgres -U <provisioning account> \
--        -f packages/ingpipe_lib/src/ingpipe_lib/sql/create_ingestion_test_db.sql
--
-- The role and database names are psql variables, defaulting to the
-- standard pair, so a bare invocation provisions ingestion_test. They
-- exist so this script can be proved end to end against a throwaway pair
-- without touching the real one -- the same rationale as
-- -v expect_database= in instances/policy_db/sql/mcp_ro_policy_grants.sql:
--   psql -d postgres -U <provisioning account> \
--        -v role_name=ingestion_test_runner_scratch \
--        -v db_name=ingestion_test_scratch \
--        -f packages/ingpipe_lib/src/ingpipe_lib/sql/create_ingestion_test_db.sql
--
-- Privilege model
--   invoking account       Must be able to (a) CREATE DATABASE owned by
--                          the runner role, and (b) install the extensions
--                          below. A superuser satisfies both, but is not
--                          the only option and is NOT assumed -- each is
--                          asserted separately, and only when this run
--                          actually needs it.
--                          (a) is the one that surprises: on PostgreSQL
--                          16+ the creator must be able to SET ROLE to
--                          the owner, and a CREATEROLE account is granted
--                          admin_option on the roles it creates but not
--                          set_option (createrole_self_grant defaults to
--                          empty) -- so having just run the role script
--                          is not enough. The remedy is a self-grant, and
--                          the precondition below prints it verbatim. The
--                          script does NOT perform that grant itself:
--                          handing the invoking account SET on the
--                          test-runner role is a privilege change, and it
--                          stays a deliberate act by the operator rather
--                          than a side effect of provisioning.
--   ingestion_test         owned by ingestion_test_runner; holds nothing
--                          but the suite's short-lived schemas. Ownership
--                          implies CREATE, which is all the fixtures need.
--
-- Extension contract: the engine VERIFIES extensions (ingpipe_lib.db.
-- require_extensions) and never creates them, so provisioning installs here
-- everything the engine's DDL relies on -- ltree (file/excel ingestion
-- identity) and vector (pgvector, embedding tables). vector is not a trusted
-- extension, so installing it into a fresh database needs a superuser; the
-- alternative is to seed the cluster's template1 once, after which new
-- databases inherit both. The precondition below names both routes rather
-- than letting the raw permission error name neither. The instance-side
-- equivalent lives in each instance's sql/ scripts.

-- Abort on the first failed statement: a half-provisioned database must
-- not hide behind a zero exit.
\set ON_ERROR_STOP on

-- Defaults for the object names, applied only when not supplied on the
-- command line via -v (see the header).
\if :{?role_name}
\else
    \set role_name ingestion_test_runner
\endif
\if :{?db_name}
\else
    \set db_name ingestion_test
\endif

-- The names travel into the precondition blocks through GUCs because psql
-- does not interpolate variables inside a dollar-quoted body.
set provisioning.role_name = :'role_name';
set provisioning.db_name = :'db_name';

-- --- precondition: the owner role exists, and the account can proceed --
-- The role is this script's input, not its product: it must already exist
-- (create_ingestion_test_role.sql creates it). CREATE DATABASE is
-- attribute-gated, and the attribute is read from current_user's own
-- pg_roles row: role attributes are NOT inherited through membership.
-- Asserted only when the database is absent -- a re-run against an
-- existing database executes no DDL and legitimately requires nothing,
-- which is the documented way to add a newly required extension.
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
        using hint = 'Run create_ingestion_test_role.sql first.';
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

-- Ownership implies CREATE on the database, so the role can create and drop
-- the suite's ephemeral schemas with no further grants. (CREATE DATABASE
-- cannot run inside a transaction block, so this script is not wrapped in
-- begin/commit; ON_ERROR_STOP still prevents it running after a failed
-- precondition.) Created only when absent so a re-run is a no-op here.
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
-- extension "vector"`, which is accurate but names no way forward. Only
-- extensions that are BOTH missing and un-installable by this account are
-- reported, so a database that already has them (e.g. inherited from a seeded
-- template1) passes regardless of who is connected.
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
    -- extension needs only CREATE on the database; an untrusted one (vector)
    -- needs a superuser.
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
