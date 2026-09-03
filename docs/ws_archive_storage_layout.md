# WS archive storage layout

The only cold WS archive artifact is Parquet with ZSTD compression.  Archive metadata is
centralized and each completed chunk permanently records the named storage root that owns its
relative artifact path.

Configure the archive explicitly; there is no implicit directory beside the HOT SQLite database:

```text
LIVE15_WS_ARCHIVE_ROOTS=parquet-01=D:\LIVE15_ARCHIVE\parquet-01;parquet-02=D:\LIVE15_ARCHIVE\parquet-02;parquet-03=D:\LIVE15_ARCHIVE\parquet-03;parquet-04=D:\LIVE15_ARCHIVE\parquet-04
LIVE15_WS_ARCHIVE_ACTIVE_ROOT=parquet-01
LIVE15_WS_ARCHIVE_MANIFEST_PATH=D:\LIVE15_ARCHIVE\manifest\ws_archive_manifest.sqlite3
LIVE15_ENABLE_WS_ARCHIVE=true
```

All configured roots and the active root must be present together with the manifest path.  Root
IDs and absolute root paths must be unique, and the manifest cannot be stored inside an archive
root.  `LIVE15_WS_ARCHIVE_ACTIVE_ROOT` selects the single writer destination only; it does not
move historical chunks, trigger balancing, fail over, or add a worker.

On read, replay verification, and purge authorization, LIVE15 resolves every chunk by its
manifest `storage_root_id` plus its relative path.  An unknown root ID, a missing root, or an
escaping/non-Parquet path fails closed before verification or deletion.  A valid chunk on an older
root remains readable after a later active-root change, provided that named root is still
configured and available.
