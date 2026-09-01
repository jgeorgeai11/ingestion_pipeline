-- mcp_ro_policy_grants.sql
--
-- The complete privilege model for mcp_ro_policy — the read-only account
-- behind the policy_db MCP server instance. One file per role: everything
-- this identity can reach, in all three served schemas, is here.
--
-- This is the DB-enforced backing for the server's read-only guarantee: the
-- role holds SELECT and nothing else, so run_sql cannot write no matter
-- what SQL a client sends. Keep this file in sync with the live grants.
--
-- Restores the model after a database rebuild (DROP DATABASE drops all
-- database-, schema-, and table-level grants; the LOGIN role itself is
-- cluster-level and survives). Note the MCP server is operated from a
-- separate repo (mcp_deployment); these grants live here because the
-- objects they target do.
--
-- Privilege model
--   database  CONNECT (PUBLIC's default CONNECT and TEMPORARY are revoked
--             inline below, as is PUBLIC's CREATE on schema public).
--   public    USAGE on the schema only (no CREATE, no SELECT) — see the
--             comment at that grant for why it is required rather than
--             incidental.
--   cms_iom   USAGE on the schema; SELECT on ALL tables, plus default
--             privileges for future ones.
--   qpp_cm      USAGE on the schema; SELECT on ALL tables, plus default
--             privileges for future ones.
--   usc       USAGE on the schema; SELECT on ALL tables, plus default
--             privileges for future ones.
--   removed   EXECUTE on ALL NINE large-object mutating functions is revoked
--             from PUBLIC, closing the one path by which a role documented as
--             SELECT-only could put bytes into the database.
--   bounded   Role-scoped statement/lock/idle/temp-file limits (accident
--             bounds, not security controls — see their comment).
--
-- Invocation (the schema names are psql variables, defaulting to cms_iom /
-- qpp_cm / usc; pass -v explicitly to override. The target database is taken
-- from the connection, so -d alone selects it):
--   psql -v cms_iom_schema=cms_iom -v qpp_cm_schema=qpp_cm -v usc_schema=usc \
--        -d policy_db -U <superuser> \
--        -f instances/policy_db/sql/mcp_ro_policy_grants.sql
--
-- Preconditions:
--   * The mcp_ro_policy role already exists — created by
--     create_policy_db.sql when -v serving_password is passed (the
--     password is a secret, supplied at run time and NOT stored here).
--   * Run AFTER the ingestion pipelines have created the schemas — every
--     grant target must already exist (ON_ERROR_STOP aborts otherwise).
--   * Run as a SUPERUSER, while connected to the target database (the
--     database name is read from the connection, not passed in). The database
--     owner is no longer sufficient: REVOKE on a pg_catalog function requires
--     ownership of that function (which belongs to the bootstrap superuser),
--     and ALTER ROLE ... IN DATABASE ... SET for a role you did not create
--     requires superuser. Both were added deliberately; the cost is that this
--     script joins create_policy_db.sql's extension install as a superuser
--     step.
--   * EVERY relation in the three served schemas must be owned by
--     policy_db_maintainer. `GRANT ... ON ALL TABLES IN SCHEMA` silently skips
--     nothing — it FAILS on a relation the grantor does not own, and because
--     the whole model runs in one transaction, one foreign-owned table aborts
--     the entire script rather than leaving a partial grant.
--   * Re-run this script AFTER ANY SCHEMA DROP and BEFORE the embedding step.
--     Dropping a schema deletes its pg_default_acl row, so the future-tables
--     coverage below silently stops applying to anything created afterwards;
--     and the per-database function ACLs the large-object revokes write live
--     in that database's pg_proc, so DROP DATABASE discards them too.
--   * Schema public still exists — the stock default. The PUBLIC hardening
--     below names it, so a database that has dropped it aborts the run.
--   * Grants are additive and idempotent: nothing this role already holds is
--     revoked first (the only REVOKEs target PUBLIC's defaults), so
--     re-running to reprovision after new tables are added is safe. Run
--     against a non-rebuilt DB and any stale grants on these tables would
--     persist.
--   * The two DO blocks below assert the preconditions this file's central
--     claim depends on — that it is connected to the intended database, and
--     that the role holds no attribute or role membership that would reach
--     past the per-schema model documented here.
--   * The whole model is applied all-or-nothing: GRANT, REVOKE, and ALTER
--     DEFAULT PRIVILEGES are transactional, so a failure part-way through
--     leaves the previous privileges untouched rather than half-updated.

-- Abort on the first failed statement: a missing role or a not-yet-created
-- target must not leave a half-applied privilege model behind a zero exit.
\set ON_ERROR_STOP on

-- Defaults for the schema variables, applied only when not supplied on the
-- command line via -v. Lets a bare invocation work against the standard
-- names.
\if :{?cms_iom_schema}
\else
    \set cms_iom_schema cms_iom
\endif
\if :{?qpp_cm_schema}
\else
    \set qpp_cm_schema qpp_cm
\endif
\if :{?usc_schema}
\else
    \set usc_schema usc
\endif
-- The database this script is FOR. It exists as a variable for exactly one
-- reason: so the script can be verified end-to-end against a throwaway
-- database without touching policy_db. A bare invocation gets the default and
-- therefore aborts if pointed at the wrong database.
\if :{?expect_database}
\else
    \set expect_database policy_db
\endif
-- The role this script grants to. Same rationale as expect_database: the
-- default is the production identity, and the variable exists so a scratch
-- role can be used to prove the script's behavior without granting anything
-- to the real one.
\if :{?role}
\else
    \set role mcp_ro_policy
\endif
-- The role that CREATES the served tables — the default-privileges target
-- below. Same rationale again, and it also covers a database whose owner is
-- not the standard maintainer (a dev instance provisioned with
-- create_policy_db.sql -v role_name=...).
\if :{?maintainer_role}
\else
    \set maintainer_role policy_db_maintainer
\endif

-- The database name is taken from the live connection rather than a -v
-- variable: the schema-level grants below always land in the connected
-- database, so a separately supplied name could only disagree with it and
-- split the privilege model across two databases with no error.
select current_database() as database \gset

-- Apply the model as a unit: without this, ON_ERROR_STOP would stop the run
-- but leave the already-autocommitted statements in place (e.g. PUBLIC
-- revoked and cms_iom granted while qpp_cm and usc are not).
begin;

-- --- precondition: the right database --------------------------------
-- The first statements below revoke CONNECT and TEMPORARY from PUBLIC and
-- CREATE from PUBLIC on schema public. Run against the wrong database, that
-- is not a no-op: it silently hardens a database this file says nothing
-- about, and (because -d selects the database) a single mistyped flag is all
-- it takes. The values travel through a GUC rather than :'expect_database'
-- because psql does not interpolate variables inside a dollar-quoted body.
set grants.expect_database = :'expect_database';
set grants.role = :'role';
do $$
begin
    if current_database() <> current_setting('grants.expect_database') then
        raise exception
            'This script provisions database %, but the connection is to %. '
            'Re-run with -d %, or pass -v expect_database=% deliberately.',
            current_setting('grants.expect_database'), current_database(),
            current_setting('grants.expect_database'), current_database();
    end if;
end
$$;

-- --- precondition: the role reaches nothing this file does not name ---
-- This file's central claim is that everything the identity can reach is
-- listed here. That claim is unverifiable while the role could carry a
-- superuser/bypassrls attribute or a single role membership: pg_read_all_data
-- alone would grant SELECT on EVERY table in every schema, silently defeating
-- the per-schema model above without changing one line of it. Verified clean
-- on 2026-08-23; this assertion is what keeps it so.
do $$
declare
    target text := current_setting('grants.role');
    attributes text[];
    memberships text;
begin
    select array_remove(array[
        case when rolsuper then 'SUPERUSER' end,
        case when rolcreatedb then 'CREATEDB' end,
        case when rolcreaterole then 'CREATEROLE' end,
        case when rolbypassrls then 'BYPASSRLS' end
    ], null)
      into attributes
      from pg_roles
     where rolname = target;

    if attributes is null then
        raise exception 'Role % does not exist; create it before running this script.', target;
    end if;

    if array_length(attributes, 1) > 0 then
        raise exception
            'Role % holds %, which reaches past the per-schema model this file documents.',
            target, array_to_string(attributes, ', ');
    end if;

    select string_agg(grantor.rolname, ', ' order by grantor.rolname)
      into memberships
      from pg_auth_members member
      join pg_roles grantor on grantor.oid = member.roleid
      join pg_roles grantee on grantee.oid = member.member
     where grantee.rolname = target;

    if memberships is not null then
        raise exception
            'Role % is a member of %, so it inherits privileges this file does not list.',
            target, memberships;
    end if;
end
$$;

-- --- database scoping -------------------------------------------------
-- PUBLIC's default CONNECT is revoked INLINE here rather than in a separate
-- public_hardening.sql (metadata_db's convention): that split exists so
-- several per-role files do not each redundantly revoke it, but policy_db
-- serves a single role, so the revoke lives in that role's file. It makes
-- CONNECT explicit — a role reaches this database only because this script
-- named it — which is also what lets role-driven list_databases show only
-- the databases this instance is actually granted.
revoke connect on database :"database" from public;
-- The next two revokes close the write paths PostgreSQL grants to PUBLIC by
-- default, which mcp_ro_policy would otherwise inherit: TEMPORARY on the
-- database allows `create temp table ... as select ...`, and (on server
-- versions before 15) CREATE on schema public allows creating and writing
-- real tables there. Without them the SELECT-only model above is a
-- convention rather than a DB-enforced guarantee.
revoke temporary on database :"database" from public;
revoke create on schema public from public;
grant connect on database :"database" to :"role";

-- --- the large-object write path --------------------------------------
-- PostgreSQL grants EXECUTE on these to PUBLIC by default, and verified
-- against the live database on 2026-08-23, mcp_ro_policy could execute all
-- nine. Any client emitting SQL could therefore call
-- `select lo_from_bytea(0, repeat('x', 1000000)::bytea)` in a loop and write
-- bytes into pg_largeobject until the disk filled — from a role this file
-- documents as SELECT-only. lo_import/lo_export are already denied (they are
-- server-side file access and superuser-only), which is why the audit's
-- finding was specifically about this family.
--
-- The set below is the WHOLE mutating surface of the large-object API, not an
-- enumeration of what one audit happened to report. Every way a client can
-- change pg_largeobject is one of five acts, and each has its entry points:
--   create      lo_creat, lo_create, lo_from_bytea
--   open for    lo_open — the descriptor lowrite and lo_truncate* both
--   writing     require; nothing in the served schemas stores a large
--               object, so there is no read use to preserve either
--   write       lowrite, lo_put
--   extend or   lo_truncate, lo_truncate64 — truncate GROWS an object when
--   shrink      the new length exceeds the old, zero-filling the difference,
--               which is the fill-the-disk path a write-only reading of the
--               name misses
--   destroy     lo_unlink
-- The read-side functions (loread, lo_get, lo_lseek/lo_lseek64, lo_tell/
-- lo_tell64, lo_close) are deliberately left alone: they cannot change a
-- byte, and revoking them would harden nothing while making the set look
-- like a blanket ban rather than a reasoned one.
--
-- These revokes are the one part of this hardening that closes a PRIVILEGE
-- rather than setting a bound: unlike the GUCs below, a client cannot grant
-- it back.
--
-- The signatures are spelled out and schema-qualified because REVOKE resolves
-- a function by exact argument types; a wrong arity is an error that, inside
-- this transaction, aborts the entire model.
revoke execute on function pg_catalog.lo_from_bytea(oid, bytea) from public;
revoke execute on function pg_catalog.lo_create(oid) from public;
revoke execute on function pg_catalog.lo_creat(integer) from public;
revoke execute on function pg_catalog.lo_open(oid, integer) from public;
revoke execute on function pg_catalog.lo_put(oid, bigint, bytea) from public;
revoke execute on function pg_catalog.lowrite(integer, bytea) from public;
revoke execute on function pg_catalog.lo_truncate(integer, integer) from public;
revoke execute on function pg_catalog.lo_truncate64(integer, bigint) from public;
revoke execute on function pg_catalog.lo_unlink(oid) from public;

-- --- schema: public ---------------------------------------------------
-- USAGE only: no CREATE (revoked from PUBLIC above) and no SELECT (nothing
-- the corpus uses lives here). It is granted EXPLICITLY rather than left
-- implicit because the search queries depend on it: the `vector` type and its
-- operator classes are installed by CREATE EXTENSION into schema public, and
-- a role without USAGE there cannot resolve `<=>` or cast to `vector`. The
-- privilege was previously inherited from PUBLIC's default USAGE, which is
-- exactly the kind of dependency that breaks silently the day someone
-- tightens the default.
grant usage on schema public to :"role";

-- --- schema: cms_iom --------------------------------------------------
grant usage on schema :"cms_iom_schema" to :"role";
grant select on all tables in schema :"cms_iom_schema" to :"role";

-- --- schema: qpp_cm -----------------------------------------------------
grant usage on schema :"qpp_cm_schema" to :"role";
grant select on all tables in schema :"qpp_cm_schema" to :"role";

-- --- schema: usc ------------------------------------------------------
grant usage on schema :"usc_schema" to :"role";
grant select on all tables in schema :"usc_schema" to :"role";

-- Future tables created by the owner are covered automatically, in all
-- three schemas. This MUST name the owning role: ALTER DEFAULT PRIVILEGES
-- only applies to objects created by the named role, and all ingestion DDL
-- runs as the maintainer. Repeated per schema because default privileges
-- are scoped to one schema each.
alter default privileges for role :"maintainer_role" in schema :"cms_iom_schema"
    grant select on tables to :"role";
alter default privileges for role :"maintainer_role" in schema :"qpp_cm_schema"
    grant select on tables to :"role";
alter default privileges for role :"maintainer_role" in schema :"usc_schema"
    grant select on tables to :"role";

-- --- resource bounds --------------------------------------------------
-- Scoped to this role IN THIS DATABASE, so nothing else on the cluster is
-- affected and a rebuild's DROP DATABASE discards them along with the rest of
-- this file's work (pg_db_role_setting rows are database-scoped).
--
-- Be honest about what these are: every one of them is a USERSET GUC, so a
-- client that can emit arbitrary SQL — which is precisely what the MCP
-- server's run_sql is — can `set statement_timeout = 0` and proceed. They
-- BOUND ACCIDENTS (a runaway analytic query, a forgotten open transaction
-- holding back vacuum, a sort that spills unboundedly), which is worth having
-- and is the common case. They are NOT an adversarial control. The binding
-- limit for a hostile or buggy client has to live on the serving process's
-- own connections, in mcp_deployment, outside this repo. The large-object
-- revokes above are different in kind: those close a privilege, and a client
-- cannot grant it back.
alter role :"role" in database :"database" set statement_timeout = '60s';
alter role :"role" in database :"database" set lock_timeout = '5s';
alter role :"role" in database :"database" set idle_in_transaction_session_timeout = '60s';
alter role :"role" in database :"database" set temp_file_limit = '1GB';

commit;
