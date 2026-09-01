---
name: cr_20260816v01_database_description
goal: Address code quality issues identified in code/grants/database_description.sql to align with sql-development skills; reviewed together with its sibling code/grants/mcp_ro_policy.sql for cross-file consistency.
created: 2026-08-16 19:32:43
updated: 2026-08-17 09:50:18
---

## Implementation Plan

1. [completed] Protect the comment text from client-encoding corruption - `code/grants/database_description.sql`
   - 1.1. [major] Line 30: the description literal contains a UTF-8 em dash (U+2014) and the file sets no client encoding. `psql` decodes file input using `client_encoding`, which on Windows defaults from the OS locale (typically WIN1252), so the three em-dash bytes are silently reinterpreted and stored as mojibake in `pg_shdescription` — corrupting the exact string `list_databases` surfaces to MCP clients, with no error and a zero exit. This is the same failure class already hit by the Python leg of this work (activity `20260816v01_relocate_db_descriptions_and_grants`, decision 15a, where the platform-default read mojibaked em dashes into the catalog).
        - Current: `\set ON_ERROR_STOP on` (no encoding declaration) followed by the em-dash-bearing literal on line 30
        - Expected: `\encoding utf8` immediately after `\set ON_ERROR_STOP on`, with a comment stating that the description text is non-ASCII and must not be decoded with the platform-default client encoding
        - Resolution: Implemented as specified — added `\encoding utf8` after `\set ON_ERROR_STOP on`, with a why-comment naming the em dash, the WIN1252 platform default, and the consequence (mojibake in `pg_shdescription`, i.e. in the string `list_databases` returns). The file itself is already UTF-8 encoded, so the em dash on the (now re-wrapped) literal is unchanged. Verified live 2026-08-17: the pre-existing stored description was checked first and was NOT corrupt (codepoint U+2014 present, no `â€”`/`Ã`/U+FFFD markers), so this fix is preventive rather than a repair — the description had been applied by a path that did not go through a WIN1252 psql client. After re-applying the rewritten script with psql 18 as `policy_db_maintainer` (exit 0), `shobj_description` still returns the em dash intact at 160 characters, confirming `\encoding utf8` plus the re-wrapped adjacent literals preserve the text byte for byte through the psql path that would otherwise have mangled it.

2. [completed] Fix formatting - `code/grants/database_description.sql`
   - 2.1. [minor] Line 30: the line is 167 characters, exceeding the 100-character maximum. `COMMENT ON` takes a string constant rather than an expression, so `||` is unavailable, but adjacent string literals separated by a newline are concatenated by PostgreSQL.
        - Current: `    'Healthcare policy and regulatory reference data — CMS Internet-Only Manuals (cms_iom), QPP cost measure codes (qpp_cm), and select United States Code titles (usc)';`
        - Expected: `    'Healthcare policy and regulatory reference data — CMS Internet-Only Manuals '` / `    '(cms_iom), QPP cost measure codes (qpp_cm), and select United States Code '` / `    'titles (usc)';`
        - Resolution: Implemented as specified — split into three adjacent literals on separate lines (PostgreSQL concatenates string constants separated by whitespace containing at least one newline, so the stored text is byte-identical; the trailing space inside the first two fragments preserves the word breaks). Longest line in the file is now 82 characters.

3. [completed] Align with the sibling grants file's conventions - `code/grants/database_description.sql`
   - 3.1. [minor] Lines 17-27: `database` is a psql variable, but `COMMENT ON DATABASE` can only target the current database — the header itself says so on line 18 — so the variable can never legitimately hold anything other than the value of `-d`, and any other value only produces `ERROR: database "x" is not the current database`. Deriving the name from the connection removes a knob that can only be set wrong. The sibling `mcp_ro_policy.sql` parameterizes `database` the same way, where a mismatch is worse still because it fails silently rather than loudly; both files should take the name from the connection (see `cr_20260816v01_mcp_ro_policy.md`, finding 2.1).
        - Current: `\if :{?database}` / `\else` / `    \set database policy_db` / `\endif`
        - Expected: `select current_database() as database \gset` (with a comment noting that `COMMENT ON DATABASE` only ever applies to the connected database), and the header's invocation line simplified to drop `-v database=policy_db`
        - Resolution: Implemented as specified — replaced the `\if :{?database}` block with `select current_database() as database \gset` plus a why-comment (the variable could only ever hold the value of `-d`; anything else raises `database "x" is not the current database`), and reduced the invocation to `psql -d policy_db -U policy_db_maintainer -f ...`. The sibling `mcp_ro_policy.sql` received the identical `\gset` form and comment rationale in the same pass (see `cr_20260816v01_mcp_ro_policy.md`, finding 2.1).
   - 3.2. [minor] Lines 22-27: the two psql constructs are bare here, while the sibling `mcp_ro_policy.sql` — applied in the same rebuild step — explains both (`\set ON_ERROR_STOP on` at its lines 47-49, the `\if` default block at its lines 51-53) and carries a labeled `Preconditions:` block. This file states no privilege precondition at all; that it must run as the database owner (`policy_db_maintainer`) or a superuser appears only implicitly in the `-U` of the invocation example.
        - Current: `\set ON_ERROR_STOP on` and the `\if` block, both uncommented, with no `Preconditions:` block in the header
        - Expected: a `Preconditions:` block mirroring the sibling's (run as the database owner or a superuser; runs while connected to the target database; idempotent because `COMMENT ON` overwrites), plus one-line why-comments above the `ON_ERROR_STOP` and default-variable constructs
        - Resolution: Implemented with two deviations. (1) Added the three-bullet `Preconditions:` block in the sibling's format and a why-comment above `\set ON_ERROR_STOP on`; the second requested comment landed on the `\gset` that finding 3.1 substituted for the default-variable block, since that construct no longer exists. (2) Removed the now-duplicated "Idempotent: COMMENT ON overwrites." sentence from the header's rebuild paragraph, because the new precondition bullet states it — the sibling likewise carries idempotency only in its `Preconditions:` block.

## Skills with No Issues

1. SQL best-practices — explicit column references: N/A - no `select` of table data; the script applies a single `COMMENT ON`
2. SQL best-practices — explicit joins, group/order by, `union all`, NULL handling, CTEs: N/A - no queries in this file
3. SQL best-practices — formatting (lowercase, 4-space indent): No issues found apart from the line-length defect in finding 2.1; the statement is lowercase and the continuation line uses 4 spaces
4. SQL best-practices — comments explain why, placed above the code: No issues found in the header prose (it explains why a non-grant file lives in `grants/` and why it must be re-applied after a rebuild); the missing comments on the two psql constructs are covered by finding 3.2
5. SQL best-practices — query block annotation (`Level = <col>`): N/A - no queries or CTEs to annotate
6. dbt skill: N/A - not a dbt project
