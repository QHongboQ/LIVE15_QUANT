# LIVE15 Recorder Windows service

`LIVE15Recorder` is an independent WinSW v2.12.0 service. Its authoritative application
entrypoint is the existing `live15_quant.cli:recorder_main` console entrypoint, invoked by the
canonical project interpreter through the service XML:

```text
Windows SCM -> WinSW -> D:\LIVE15_QUANT\.venv\Scripts\python.exe
  -c "from live15_quant.cli import recorder_main; recorder_main()"
```

The Recorder owns its existing `RecorderStore`, `RecorderPidLease`, heartbeat, provider
synchronization, and archive-worker behavior. This package does not change those semantics.
WinSW owns only service startup, graceful stop, and bounded process-exit recovery:
three restart actions (10/30/60 seconds), then `none`, with a five-minute failure reset.

Production credential paths are references, never secret values. The install script requires
both `LIVE15_KALSHI_PRODUCTION_API_KEY_ID_PATH` and
`LIVE15_KALSHI_PRODUCTION_PRIVATE_KEY_PATH` (or explicit path arguments), verifies that the
files are readable, and renders the references into an ignored local service XML. The service
configuration contains no credential contents.

Control Center remains a separate `LIVE15ControlCenter` service. Neither service starts,
stops, restarts, or supervises the other. The Recorder does not depend on port 8765 or on the
UI being opened.

Run `tools/install_recorder_service.ps1 -WhatIf` to validate and stage the package without
installing a service. Administrator/UAC approval is required for the real WinSW `install`,
`start`, `stop`, or `uninstall` commands.
