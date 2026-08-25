# Project-local Python 3.13 toolchain

The LIVE15 development environment uses the official NuGet CPython package for a
sandbox-readable, project-local interpreter. This does not install Python system-wide and
does not change the Windows PATH.

| Field | Value |
| --- | --- |
| Package | `python` |
| Version | `3.13.15` |
| Source | <https://api.nuget.org/v3-flatcontainer/python/3.13.15/python.3.13.15.nupkg> |
| SHA-256 | `05357887DF50D3153EFC681BDF432C321D3E2F9CE5788F99F4515B27E8FDA0AC` |
| Package size | `14,391,248` bytes |
| Managed interpreter | `.toolchain/Python313/python.exe` |
| Project environment | `.venv/Scripts/python.exe` |
| Dependency source | `requirements.lock` (`kalshi-sdk==12.0.0`) |

`.toolchain/` is ignored by Git. The previous environment is retained at
`.venv.pre-nuget-python-20260826-20260826-014606` for rollback; it is not deleted.

The interpreter, temporary venv creation, lockfile installation, project imports, and JSON
parsing were verified. Runtime restart validation remains conditional on the normal Codex
sandbox being able to read the existing external Pyth credential file; no credential or ACL
change is part of this toolchain migration.
