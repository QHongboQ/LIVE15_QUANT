# LIVE15 Control Center service boundary

The LIVE15 Control Center is maintained by Windows Service Control Manager through
WinSW v2.12.0. The service launches the canonical project runtime directly:

```text
Windows SCM -> WinSW -> <project-root>\.venv\Scripts\python.exe -m live15_quant.control_center
```

`LIVE15ControlCenter` owns only the localhost UI/API on `127.0.0.1:8765`. WinSW
provides Automatic startup, bounded failure actions, graceful stop, and crash
recovery. The service does not own or supervise Recorder processes.

Recorder is an independent service boundary. The Control Center may display
Recorder health and issue bounded, authorized application-level commands, but it
must not start, stop, restart, or otherwise own the Recorder process lifecycle.
UI failure must not stop Recorder; Recorder failure must not stop the UI.

RUNTIME-SERVICE-001B acceptance is complete based on the Windows integration
evidence: installation with the pinned SHA256, Automatic startup, direct
canonical Python launch, HTTP 200 responses, 60-second stability, clean
stop/start, port release, no orphan process, and automatic crash recovery with
a new service-owned process instance.

Recorder runtime independence proof is deferred to the independent Recorder
service lifecycle task; Recorder availability is not a Control Center service
prerequisite.

## Production account read configuration

The Control Center uses the existing LIVE15 explicit Production credential
references when it serves the read-only account endpoints. The tracked XML keeps
the two environment entries as placeholders:

```text
LIVE15_KALSHI_PRODUCTION_API_KEY_ID_PATH=%LIVE15_KALSHI_PRODUCTION_API_KEY_ID_PATH%
LIVE15_KALSHI_PRODUCTION_PRIVATE_KEY_PATH=%LIVE15_KALSHI_PRODUCTION_PRIVATE_KEY_PATH%
```

`tools/install_control_center_service.ps1` resolves those references from the
approved host configuration (or explicit path arguments), verifies that both
files are readable, escapes the paths for XML, and renders the ignored local
service configuration. Only path references are rendered; key contents are
never copied, logged, or committed. A missing or unreadable reference fails
closed before the WinSW install command is invoked.

The superseded custom watchdog implementation is retired by
`SUPERSEDED_BY_RUNTIME_SERVICE_001`.


