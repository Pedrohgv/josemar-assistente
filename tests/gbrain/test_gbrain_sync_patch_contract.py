"""Source-contract tests for the stored gbrain issue #125 two-part patch.

These tests inspect `patches/gbrain-inline-worker-gateway.patch` (no Docker, no
gbrain binary required) and pin the EXACT narrowed invariants the W2 fix
(issue #125, maintainer-approved two-part design) must keep:

Part 1 — write-through identity (`src/core/ops/pages.ts` +
`src/core/write-through.ts`):
  - a put_page/capture write-through row gets a `source_path` ONLY when the
    write-through really wrote a file under a configured source root
    (`written === true` with a real path) — disabled/skipped/failed writes
    never stamp, so no identity is ever fabricated for a capture flow without
    a real write-through vault path;
  - ONLY a LIVE row whose `source_path` is currently NULL is stamped: the
    pre-check AND the UPDATE predicate both require `source_path IS NULL`, so
    a non-NULL identity is never overwritten (and the guarded UPDATE is
    atomic against concurrent stamps);
  - the value is the owning-source-relative path of the ACTUAL written file
    (computed via the same `resolvePageWriteTarget` the write-through used),
    refused when unrepresentable (absolute / `..` escape / non-markdown),
    forward-slash normalized;
  - scoped to the exact (source_id, slug) live row;
  - if the metadata update fails, the file/page are RETAINED, `source_path`
    stays NULL, and the failure is surfaced through the established
    write-through result contract (`sourcePathError`) plus a warning —
    identity is never silently claimed.

Part 2 — fail-closed incremental stale-file pass (`src/commands/sync.ts`):
  - runs ONLY after the whole add/modify/rename/delete phase succeeded
    (`failedFiles.length === 0`) and ONLY source-scoped (`opts.sourceId`);
  - reuses the full-sync `planReconcileDeletes` machinery verbatim (same
    normalization, scope/strategy/malformed/path-confinement predicates, and
    the #2828 mass-delete valve — no alternate algorithm);
  - rows: source-scoped LIVE non-NULL `source_path` only — NULL identity is
    never swept;
  - `listEverCommittedPaths` MUST succeed: a stale path is deleted ONLY when
    proven ever committed AND absent from the current syncable tree; if git
    history proof is unavailable the destructive pass is skipped;
    never-committed (DB-only write-through) rows are retained;
  - no hash/same-content/path-slug-guess delete authority: deletion is by the
    row's OWN slug from its recorded source_path;
  - a delete failure creates a non-skippable `<stale:…>` SENTINEL in the
    failure ledger (the #3056 `<rename:…>` precedent): hard-blocks the
    bookmark even under --skip-failed and can never be auto-skipped;
  - the mass-delete escape hatch stays env-only: nothing in the patch (or the
    refresh path) sets GBRAIN_ALLOW_MASS_RECONCILE.

If any of these guards is dropped (e.g. a rebase rewrites the hunks), these
tests fail so the invariants are never silently lost.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
PATCH_PATH = REPO_ROOT / "patches" / "gbrain-inline-worker-gateway.patch"

PAGES_HEADER = "diff --git a/src/core/ops/pages.ts b/src/core/ops/pages.ts"
WRITE_THROUGH_HEADER = (
    "diff --git a/src/core/write-through.ts b/src/core/write-through.ts"
)
SYNC_HEADER = "diff --git a/src/commands/sync.ts b/src/commands/sync.ts"

PART1_MARKER = "// JOSEMAR PATCH (issue #125, part 1): stamp the vault-relative"
PART2_MARKER = "// JOSEMAR PATCH (issue #125, part 2): fail-closed incremental stale-file"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _hunk(patch: str, file_header: str) -> str:
    """Isolate a patched file's diff region: from its `a/...` diff header to
    the next `diff --git` header (or the end of the patch)."""
    pos = patch.find(file_header)
    if pos < 0:
        raise AssertionError(f"{file_header} hunk missing from patch")
    next_diff = patch.find("\ndiff --git", pos + 1)
    return patch[pos:next_diff] if next_diff > 0 else patch[pos:]


def _hunk_last(patch: str, file_header: str) -> str:
    """Like ``_hunk`` but the LAST occurrence of the file header (a
    cumulative patch may carry several sections for the same file)."""
    pos = patch.rfind(file_header)
    if pos < 0:
        raise AssertionError(f"{file_header} hunk missing from patch")
    next_diff = patch.find("\ndiff --git", pos + 1)
    return patch[pos:next_diff] if next_diff > 0 else patch[pos:]


def _marker_region(region: str, marker: str) -> str:
    """From a diff region, isolate everything from ``marker`` to the end of
    the region (the hunk that opens with that marker)."""
    pos = region.find(marker)
    if pos < 0:
        raise AssertionError(f"{marker!r} hunk missing from patch region")
    return region[pos:]


class GbrainSyncSourcePathPatchContractTests(unittest.TestCase):
    """Part 1: the write-through identity stamp keeps its exact invariants."""

    def setUp(self) -> None:
        self.assertTrue(PATCH_PATH.is_file(), f"missing patch: {PATCH_PATH}")
        self.patch = _read(PATCH_PATH)
        self.pages = _hunk(self.patch, PAGES_HEADER)
        self.part1_section = _hunk_last(self.patch, PAGES_HEADER)
        self.region = _marker_region(self.part1_section, PART1_MARKER)
        self.wt = _hunk(self.patch, WRITE_THROUGH_HEADER)

    # --- presence ----------------------------------------------------------

    def test_part1_hunk_present_in_pages_ts_only(self) -> None:
        self.assertIn(PART1_MARKER, self.part1_section)
        for header in (
            "diff --git a/src/commands/capture.ts b/src/commands/capture.ts",
            "diff --git a/src/core/import-file.ts b/src/core/import-file.ts",
            "diff --git a/src/core/sync-git.ts b/src/core/sync-git.ts",
        ):
            self.assertNotIn(header, self.patch)
        # The write-through result contract carries the stamp-failure field.
        self.assertIn(WRITE_THROUGH_HEADER, self.patch)

    # --- narrow gating: real write-through only ----------------------------

    def test_stamp_fires_only_after_real_write_through(self) -> None:
        """The stamp must be gated on `written === true` with a real path —
        a capture flow without a real write-through vault path never gets a
        fabricated source_path (disabled / skipped / failed writes all keep
        `written` falsy or `path` absent)."""
        self.assertIn(
            "if (writeThrough?.written === true && writeThrough.path) {",
            self.region,
        )
        # After the write-through decision chain's dry-run skip (last branch).
        dry_run_pos = self.part1_section.find("writeThrough = { written: false, skipped: 'dry_run' }")
        stamp_pos = self.part1_section.find("if (writeThrough?.written === true && writeThrough.path) {")
        self.assertGreater(dry_run_pos, 0, "write-through decision chain missing")
        self.assertGreater(stamp_pos, dry_run_pos, "stamp must run after the write-through decision chain")

    def test_stamp_precedes_auto_link_post_hook(self) -> None:
        self.assertIn("    // Auto-link post-hook: runs AFTER importFromContent", self.part1_section)
        stamp_pos = self.part1_section.find("if (writeThrough?.written === true && writeThrough.path) {")
        auto_link_pos = self.part1_section.find("    // Auto-link post-hook: runs AFTER importFromContent")
        self.assertLess(stamp_pos, auto_link_pos)

    # --- never overwrite non-NULL identity --------------------------------

    def test_stamp_never_overwrites_non_null_identity(self) -> None:
        """The pre-check AND the UPDATE predicate both require
        `source_path IS NULL`: a row that already has an identity is never
        overwritten, and a concurrent stamp cannot clobber (the guarded
        UPDATE is atomic)."""
        self.assertIn(
            "SELECT source_path FROM pages WHERE source_id = $1 AND slug = $2 AND deleted_at IS NULL LIMIT 1",
            self.region,
        )
        self.assertIn("row[0].source_path == null", self.region)
        self.assertIn(
            "UPDATE pages SET source_path = $1 WHERE source_id = $2 AND slug = $3 AND deleted_at IS NULL AND source_path IS NULL",
            self.region,
        )

    # --- canonical relative form ------------------------------------------

    def test_stamp_uses_actual_written_path_and_write_root(self) -> None:
        self.assertIn("const { resolvePageWriteTarget } = await import('../write-through.ts');", self.region)
        self.assertIn("resolvePageWriteTarget(ctx.engine, result.slug, stampSourceId)", self.region)
        self.assertIn("const rel = relative(resolve(target.writeRoot), resolve(writeThrough.path));", self.region)
        self.assertIn("rel.replace(/\\\\/g, '/')", self.region)

    def test_stamp_refuses_unsafe_relative_paths(self) -> None:
        self.assertIn("isAbsolute(rel)", self.region)
        self.assertIn("segment === '..'", self.region)
        self.assertIn("rel.toLowerCase().endsWith('.md')", self.region)

    def test_stamp_unrepresentable_path_surfaces_error(self) -> None:
        """An unrepresentable written path must NOT establish identity and
        must surface through the result contract, not stay silent."""
        self.assertIn("is not a representable source-relative markdown path", self.region)
        self.assertIn("writeThrough.sourcePathError = errMsg", self.region)

    # --- scoping -----------------------------------------------------------

    def test_stamp_update_is_scoped_to_exact_row(self) -> None:
        self.assertIn("stampSourceId", self.region)
        self.assertIn("result.slug", self.region)
        # Exactly one UPDATE, always the fully scoped NULL-guarded form —
        # no bare-slug write anywhere in the hunk.
        self.assertEqual(self.region.count("UPDATE pages SET source_path = $1"), 1)

    # --- best-effort contract ---------------------------------------------

    def test_stamp_failure_retains_and_surfaces(self) -> None:
        """A metadata-update failure retains file/page, leaves source_path
        NULL, and is exposed through the write-through result contract
        (`sourcePathError`) plus a warning — identity never silently
        claimed."""
        self.assertIn("row still has no source_path after update", self.region)
        self.assertIn("writeThrough.sourcePathError = errMsg", self.region)
        self.assertIn("ctx.logger.warn(", self.region)
        self.assertIn("} catch (e) {", self.region)

    def test_write_through_contract_carries_source_path_error(self) -> None:
        """The established WriteThroughResult contract gains the documented
        optional `sourcePathError` field (no schema/public CLI change)."""
        self.assertIn("sourcePathError?: string;", self.wt)
        self.assertIn("source_path", self.wt)

    # --- existing hunk interaction ----------------------------------------

    def test_existing_no_embed_hunk_still_present(self) -> None:
        self.assertIn(
            "const noEmbed = cfgDisabled || !isAvailable('embedding')",
            self.pages,
        )
        self.assertNotIn("const noEmbed = cfgDisabled", self.part1_section)

    def test_no_minted_source_path_in_import_call(self) -> None:
        """The hunk must not thread a minted sourcePath into
        importFromContent opts (no `sourcePath:` key anywhere in the pages
        region — only the SQL column and writeThrough.path are used)."""
        self.assertNotIn("sourcePath:", self.region)


class GbrainSyncStalePassPatchContractTests(unittest.TestCase):
    """Part 2: the fail-closed incremental stale-file pass keeps its exact
    safety invariants."""

    def setUp(self) -> None:
        self.patch = _read(PATCH_PATH)
        self.sync = _hunk(self.patch, SYNC_HEADER)
        self.region = _marker_region(self.sync, PART2_MARKER)

    def test_part2_hunk_present_in_sync_ts(self) -> None:
        self.assertIn(PART2_MARKER, self.sync)

    def test_pass_runs_only_after_full_phase_success_and_source_scoped(self) -> None:
        """The pass must be gated on an empty failure ledger (any add/modify/
        rename/delete/postcondition failure skips it) and on a resolved
        sourceId (source-scoped only)."""
        self.assertIn("if (failedFiles.length === 0 && opts.sourceId) {", self.region)
        self.assertIn("const sid = opts.sourceId;", self.region)

    def test_pass_positioned_before_bookmark_advancement(self) -> None:
        """The pass runs after the head-verification postcondition block and
        BEFORE the bookmark/checkpoint advance (const elapsed / advance)."""
        # The pass is inserted after the head-verification catch block (the
        # hunk's leading context is the catch's closing brace) and BEFORE the
        # bookmark advancement (const elapsed precedes the failure-gate
        # advance()).
        marker_pos = self.sync.find(PART2_MARKER)
        elapsed_pos = self.sync.find("const elapsed = Date.now() - start;")
        self.assertGreater(elapsed_pos, 0)
        self.assertLess(marker_pos, elapsed_pos, "pass must run before bookmark advancement")

    def test_never_sweeps_null_identity(self) -> None:
        self.assertIn("source_path IS NOT NULL AND deleted_at IS NULL", self.region)

    def test_reuses_full_sync_reconcile_machinery(self) -> None:
        """The pass reuses planReconcileDeletes + collectSyncableFiles +
        listEverCommittedPaths + massReconcileAllowed verbatim — no
        alternate algorithm."""
        self.assertIn("planReconcileDeletes(", self.region)
        self.assertIn("collectSyncableFiles(syncScopeRoot, {", self.region)
        self.assertIn("listEverCommittedPaths(gitContextRoot)", self.region)
        self.assertIn("massReconcileAllowed()", self.region)

    def test_scope_strategy_malformed_confinement_preserved(self) -> None:
        self.assertIn("reconcileEligible", self.region)
        self.assertIn("isSyncable(p, reconcileSyncOpts)", self.region)
        self.assertIn("'malformed-path' && isPoisonedPath(p)", self.region)
        self.assertIn("scopePrefix", self.region)

    def test_mass_delete_valve_trips_skip(self) -> None:
        self.assertIn("plan.massDelete && !massReconcileAllowed()", self.region)
        self.assertIn("No pages were deleted.", self.region)

    def test_mass_escape_hatch_never_settable_by_patch(self) -> None:
        """GBRAIN_ALLOW_MASS_RECONCILE is read-only in the patch: nothing may
        assign it (the automatic refresh path can never bypass the valve)."""
        self.assertNotIn("GBRAIN_ALLOW_MASS_RECONCILE=", self.patch)
        self.assertNotIn("GBRAIN_ALLOW_MASS_RECONCILE =", self.patch)

    def test_ever_committed_proof_required(self) -> None:
        self.assertIn("everCommitted === null", self.region)
        self.assertIn("no rows deleted", self.region)
        # Never-committed rows are retained, never deleted.
        self.assertIn("retained.push(slug)", self.region)
        self.assertIn("not deleting", self.region)

    def test_falsy_source_path_explicitly_retained(self) -> None:
        """The retained/deletable split must explicitly retain a falsy or
        empty source_path (fail-closed): no recorded identity proof means no
        delete authority — only a non-empty path that was ever committed AND
        is absent from the current tree may flow to deletion."""
        self.assertIn(
            "if (!sp || !everCommitted.has(sp.replace(/\\\\/g, '/'))) retained.push(slug);",
            self.region,
        )
        self.assertIn("else deletable.push(slug);", self.region)
        # The fail-closed rationale is documented at the split itself.
        self.assertIn("no delete authority", self.region)
        self.assertIn("a falsy/empty source_path is explicitly RETAINED", self.region)

    def test_no_slug_guess_or_hash_delete_authority(self) -> None:
        """Deletion is by the row's OWN slug from its recorded source_path:
        no resolveSlugForPath fallback and no content-hash comparison in the
        pass."""
        self.assertNotIn("resolveSlugForPath(", self.region)
        self.assertNotIn("content_hash", self.region)

    def test_delete_failure_creates_nonskippable_sentinel(self) -> None:
        """A delete failure creates a `<stale:…>` sentinel in the failure
        ledger (the #3056 `<rename:…>` precedent) — hard-blocks the bookmark
        and is never auto-skippable."""
        self.assertIn("path: `<stale:${sp}>`", self.region)
        self.assertIn("failedFiles.push({", self.region)
        self.assertIn("stale-file delete failed for", self.region)
        self.assertIn("SENTINEL", self.region)

    def test_delete_success_clears_stale_sentinel(self) -> None:
        """Once a delete converges on a later run, the `<stale:…>` sentinel
        clears through the ordinary success path (succeededPaths) exactly
        like `<rename:…>` — a transient delete outage never ages doctor to a
        permanent FAIL."""
        self.assertIn("succeededPaths.push(`<stale:${pathBySlug.get(s) ?? s}>`)", self.region)
        self.assertIn("succeededPaths.push(`<stale:${pathBySlug.get(slug) ?? slug}>`)", self.region)
        self.assertIn("self-heals through the ordinary success path", self.region)

    def test_unexpected_prep_failure_creates_self_clearing_sentinel(self) -> None:
        """F2 (merge-blocking finding): an UNEXPECTED stale-pass
        preparation/enumeration/planning failure (the outer catch) must add
        a DEDICATED non-skippable sentinel through the existing failure
        ledger — it hard-blocks the bookmark and self-clears once the pass
        completes cleanly on a later run. The intentional safe skips
        (git-history proof unavailable, mass valve, retention) stay
        DISTINCT: they are not errors and never enter the ledger."""
        # The outer catch records the dedicated sentinel (never auto-skipped:
        # `<`-prefixed → non-skippable in the shared gate).
        self.assertIn("path: `<stale-prep:${sid}>`", self.region)
        self.assertIn("stale-file pass preparation failed", self.region)
        self.assertIn("failedFiles.push({", self.region)
        self.assertIn("stalePrepFailed = true;", self.region)
        # The pass that RAN to completion (safe skips included) clears any
        # previous prep sentinel through the ordinary success path.
        self.assertIn("succeededPaths.push(`<stale-prep:${sid}>`)", self.region)
        self.assertIn("if (!stalePrepFailed) succeededPaths.push", self.region)
        # The safe outcomes remain serr-only (no failedFiles entry).
        self.assertIn("git history proof unavailable", self.region)
        self.assertIn("mass-delete valve", self.region)
        self.assertIn("RETAINED, never deleted", self.region)

    def test_source_scoped_native_delete(self) -> None:
        self.assertIn("engine.deletePages(batch, { sourceId: sid })", self.region)
        self.assertIn("engine.deletePage(slug, { sourceId: sid })", self.region)

    def test_rename_applied_follows_source_path(self) -> None:
        """Part-2 complement: when the cheap rename (updateSlug) applies, the
        row's source_path must follow the ACTUAL git rename destination —
        otherwise any source_path-based reconcile (the stale pass, the
        full-sync reconcile) would sweep the renamed LIVE row (its recorded
        path is gone). Scoped to the exact (source_id, new slug) live row."""
        self.assertIn("if (renameApplied) {", self.sync)
        self.assertIn(
            "UPDATE pages SET source_path = $1 WHERE source_id = $2 AND slug = $3 AND deleted_at IS NULL",
            self.sync,
        )
        self.assertIn(
            "[to.replace(/\\\\/g, '/'), opts.sourceId ?? DEFAULT_SOURCE_ID, newSlug]",
            self.sync,
        )
        # The follow runs right after updateSlug, BEFORE the reimport.
        follow_pos = self.sync.find("if (renameApplied) {")
        reimport_pos = self.sync.find("// Reimport at new path")
        self.assertGreater(follow_pos, 0)
        self.assertGreater(reimport_pos, follow_pos)

    def test_rename_follow_failure_is_fail_closed(self) -> None:
        """A failed identity follow must not silently converge: a
        non-skippable `<rename:…>` sentinel blocks the bookmark and disables
        the stale pass for the run (the next run retries from the same
        diff)."""
        self.assertIn("path: `<rename:${to}>`", self.sync)
        self.assertIn("rename source_path follow failed for", self.sync)
        self.assertIn("failedFiles.push({", self.sync)

    def test_rename_follow_failure_integrates_existing_convergence_state(self) -> None:
        """F1 (merge-blocking finding): the follow failure must feed the
        EXISTING rename convergence/checkpoint gates — no separate retry
        machinery. The shared `reconcileFailed` flag is initialized BEFORE
        the follow and set in its catch (and the later duplicate
        declaration is removed), so the existing
        `if (!reconcileFailed) succeededPaths.push(<rename:…>)` /
        `if (!reconcileFailed) await markCompleted(to)` gates correctly
        skip on follow failure:

          - `succeededPaths.push(`<rename:${to}>`)` is skipped (the follow
            does NOT clear its own sentinel via the success path);
          - `markCompleted(to)` is skipped (the destination is NOT banked —
            otherwise the next run's resume filter `completed.has(r.to)`
            would skip the rename, the follow would never retry, and the
            row would keep the wrong source_path forever);
          - the non-skippable `<rename:…>` sentinel hard-blocks the
            bookmark, so the next run replays the destination rename from
            the same diff and converges/clears through the success path."""
        # The shared flag is hoisted before the follow block.
        follow_pos = self.sync.find("if (renameApplied) {")
        self.assertGreater(follow_pos, 0)
        decl_pos = self.sync.rfind("let reconcileFailed = false;", 0, follow_pos)
        self.assertGreater(decl_pos, 0, "reconcileFailed must be initialized before the follow")
        # The follow catch sets it (in addition to the sentinel).
        catch_pos = self.sync.find("} catch (followErr) {", follow_pos)
        self.assertGreater(catch_pos, 0)
        between = self.sync[catch_pos:catch_pos + 600]
        self.assertIn("reconcileFailed = true;", between)
        self.assertIn("path: `<rename:${to}>`", between)
        # The later duplicate declaration is GONE (the #3056 block now uses
        # the hoisted flag — the patch removes the old `let` line).
        self.assertIn("-      let reconcileFailed = false;", self.sync)
        # The hoisted declaration appears exactly once as an added line.
        added_decls = self.sync.count("+      let reconcileFailed = false;")
        self.assertEqual(added_decls, 1)
        # No separate retry sentinel is invented for the follow.
        self.assertNotIn("<rename-retry", self.sync)

    def test_part2_hunk_line_counts_consistent(self) -> None:
        marker_pos = self.sync.find(PART2_MARKER)
        header_start = self.sync.rfind("\n@@ ", 0, marker_pos)
        self.assertGreater(header_start, 0, "part-2 hunk header not found")
        header_line = self.sync[header_start + 1 : self.sync.find("\n", header_start + 1)]
        m = re.match(r"@@ -(\d+),(\d+) \+(\d+),(\d+) @@", header_line)
        self.assertIsNotNone(m, f"malformed part-2 hunk header: {header_line!r}")
        assert m is not None
        old_lines, new_lines = int(m.group(2)), int(m.group(4))
        body = self.sync[self.sync.find("\n", header_start + 1) + 1 :].splitlines()
        added = sum(1 for ln in body if ln.startswith("+") and not ln.startswith("+++"))
        removed = sum(1 for ln in body if ln.startswith("-") and not ln.startswith("---"))
        self.assertEqual(added, new_lines - old_lines + removed)


class GbrainSyncSourcePathRebaseGuardTests(unittest.TestCase):
    """Guard rails that fail loudly if a future rebase rewrites the hunks'
    structural anchors."""

    def setUp(self) -> None:
        self.patch = _read(PATCH_PATH)

    def test_single_part1_and_part2_markers(self) -> None:
        self.assertEqual(self.patch.count(PART1_MARKER), 1)
        self.assertEqual(self.patch.count(PART2_MARKER), 1)

    def test_part1_is_last_pages_hunk_after_no_embed(self) -> None:
        pages = _hunk(self.patch, PAGES_HEADER)
        part1_section = _hunk_last(self.patch, PAGES_HEADER)
        no_embed_pos = pages.find("const noEmbed = cfgDisabled")
        marker_pos = part1_section.find(PART1_MARKER)
        self.assertGreater(no_embed_pos, 0)
        self.assertGreater(marker_pos, 0)
        # The part-1 section is a SECOND pages.ts section after the noEmbed one.
        self.assertLess(self.patch.find(PAGES_HEADER), self.patch.rfind(PAGES_HEADER))
        self.assertNotIn("const noEmbed = cfgDisabled", part1_section)


if __name__ == "__main__":
    unittest.main()
