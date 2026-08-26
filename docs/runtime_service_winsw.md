# LIVE15 Control Center service boundary

The LIVE15 Control Center is maintained by Windows Service Control Manager through
WinSW v2.12.0. The service launches the canonical project runtime directly:

```text
Windows SCM -> WinSW -> D:\LIVE15_QUANT\.venv\Scripts\python.exe -m live15_quant.control_center
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
stop/start, port release, no orphan process, and automatic crash recovery
(PID 1612 to PID 1988).

Recorder runtime independence proof is deferred to the independent Recorder
service lifecycle task; Recorder availability is not a Control Center service
prerequisite.

The superseded custom watchdog implementation is retired by
`SUPERSEDED_BY_RUNTIME_SERVICE_001`.

