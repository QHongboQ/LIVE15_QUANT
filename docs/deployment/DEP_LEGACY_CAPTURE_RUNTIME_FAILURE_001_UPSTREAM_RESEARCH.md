# DEP-LEGACY-CAPTURE-RUNTIME-FAILURE-001 upstream research

## Question and evidence boundary

This note investigates the first-deployment legacy-capture failure only.  The
bounded host evidence says that an otherwise unknown top-level transient
directory, `.checker-pytest-candidate`, was a normal (non-reparse) directory
whose ACL could not be read by the deployment account.  The original capture
stderr was not retained.  A safe synthetic reproduction produced
`shutil.Error` containing an `Access is denied`/`PermissionError` error for an
unreadable directory.

Accordingly, this is **not established as a junction, reparse-point, symlink,
or special-file failure**.  It is the normal inaccessible-directory failure
class.  The absence of the original stderr means we must not claim the exact
Windows error number, call, or path from the failed attempt.  Reparse points
are still a separate safety concern because their traversal can leave the
intended source tree.

## Primary-source findings

1. Python documents that `copytree()` is recursive; its `ignore` callback is
   called for each directory with that directory's immediate contents and its
   returned names are not copied.  Exceptions are aggregated into
   `shutil.Error`, whose argument contains `(source, destination, exception)`
   triples.  `ignore_patterns()` is the supported name-based ignore callback.
   The default is `symlinks=False`, which copies the link target rather than
   preserving a symbolic link.  A dangling target is an error unless the
   caller explicitly sets `ignore_dangling_symlinks=True`.
   [Python `shutil.copytree` documentation](https://docs.python.org/3/library/shutil.html#shutil.copytree)
   and [Python `shutil.Error` documentation](https://docs.python.org/3/library/shutil.html#shutil.Error).

2. Current CPython first scans the current source directory, invokes `ignore`
   before processing its entries, and skips a name returned by `ignore` before
   recursing.  Thus the fixed top-level capture exclusions are an effective
   no-descent boundary for those exact names.  CPython catches `OSError` while
   processing an unignored entry, records source/destination/error text, and
   raises aggregated `shutil.Error` after the tree walk.  It does not turn an
   inaccessible unknown entry into a successful copy.
   [Current `Lib/shutil.py` `_copytree` implementation](https://github.com/python/cpython/blob/main/Lib/shutil.py#L3105-L3223)
   and [current `copytree` entry point](https://github.com/python/cpython/blob/main/Lib/shutil.py#L3225-L3297).

3. Current CPython has Windows-specific junction behavior: a directory
   junction appears as a symlink to `DirEntry`; if its reparse tag is
   `IO_REPARSE_TAG_MOUNT_POINT`, `copytree()` resets its local `is_symlink`
   flag and recurses into it.  That behavior applies even where the caller
   selected `symlinks=True`; it is therefore unsuitable as a boundary that
   promises a capture will stay within the physical source tree.
   [Current Windows junction branch in `Lib/shutil.py`](https://github.com/python/cpython/blob/main/Lib/shutil.py#L3134-L3189).

4. CPython's active Windows issue documents a concrete `copytree()` junction
   hazard: recursively following a junction while copying over a destination
   junction can mutate the junction target.  It is evidence for treating
   junction traversal as a security/integrity boundary rather than relying on
   generic `copytree` behavior.
   [CPython issue #104046](https://github.com/python/cpython/issues/104046).
   A separate open CPython issue records that Windows junction traversal also
   differs in `os.walk`, confirming this is a platform-specific traversal
   concern rather than an ACL diagnosis.
   [CPython issue #67596](https://github.com/python/cpython/issues/67596).
   CPython also exposes `DirEntry.is_junction()` on Windows by checking the
   mount-point reparse tag; the related implementation change is merged.
   [CPython issue #108717](https://github.com/python/cpython/issues/108717)
   and [merged PR #108718](https://github.com/python/cpython/pull/108718).

5. Microsoft defines an NTFS reparse point as tagged data interpreted by a
   file-system filter; opening it can invoke that filter and fails when no
   matching filter exists.  Microsoft further states that junctions are links
   between distinct directories and are implemented through reparse points.
   [Microsoft: Reparse points](https://learn.microsoft.com/en-us/windows/win32/fileio/reparse-points)
   and [Microsoft: Hard links and junctions](https://learn.microsoft.com/en-us/windows/win32/fileio/hard-links-and-junctions).

6. Python documents that unsupported special files such as named pipes cause
   `SpecialFileError` from `copyfile()`/`copytree()`.  Such an entry is not in
   the bounded host evidence, but is another reason not to quietly discard an
   unrecognised top-level entry.
   [Python `SpecialFileError` documentation](https://docs.python.org/3/library/shutil.html#shutil.SpecialFileError).

No broader implementation reference is required: CPython and Microsoft own
the relevant semantics.

## Safe capture recommendation

1. Retain the exact, fixed legacy-capture exclusions already justified by
   deployment ownership: `.worktrees`, `releases`, `bootstrap`, mutable/data/
   runtime/secret paths, and `active-release.json` / `previous-release.json`.
   Pass these as `copytree(ignore=...)` names so the known boundary is excluded
   before recursive descent.  Do not add a wildcard or an exclusion inferred
   from a failed directory name.

2. For every **unexcluded** entry, inspect its type without following it.
   Reject a symbolic link or any Windows reparse point (including a junction)
   with a `ReleaseError` that reports only its relative path and classification.
   Do not delegate that decision to default `copytree`: CPython deliberately
   recurses through directory junctions.  This is a fail-closed rejection, not
   an exclusion or an attempt to preserve/copy the link target.

3. Continue to copy ordinary unexcluded entries.  If a permission, special
   file, or other copy error occurs, fail closed and surface a deterministic,
   actionable diagnostic containing the relative source path, entry class, and
   underlying Windows/Python error.  Preserve the original `shutil.Error`
   details as the cause; do not retry with elevated access, suppress errors, or
   omit the unknown entry.

4. Add synthetic-only regressions for (a) an unreadable normal directory,
   demonstrating actionable fail-closed propagation, and (b) a mocked or
   platform-guarded reparse/junction classification, demonstrating rejection
   before any descent.  Keep the existing synthetic `.worktrees` no-descent and
   same-root self-capture tests.  No test should read a real project
   `.worktrees` directory or frozen holdout.

This approach preserves `LEGACY_UNPROVEN_ROLLBACK_ARTIFACT`, `git_commit_sha =
UNPROVEN`, and `source_tree_identity = UNPROVEN`: it changes only which host
trees are eligible to become that artifact, never its provenance semantics.
