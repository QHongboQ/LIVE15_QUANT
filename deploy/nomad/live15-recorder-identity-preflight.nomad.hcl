# Bounded, non-authoritative identity preflight for a future Recorder gate.
# It never imports or invokes recorder_main and is not submitted in PREP-002.

variable "recorder_runtime_python" {
  type        = string
  description = "Absolute protected CPython executable for the reviewed candidate."
}

variable "recorder_app_root" {
  type        = string
  description = "Absolute immutable Recorder release app root."
}

variable "recorder_work_dir" {
  type        = string
  description = "Existing mutable Recorder working directory."
}

variable "kalshi_api_key_id_path" {
  type        = string
  description = "Absolute external Kalshi API-key identifier path."
}

variable "kalshi_private_key_path" {
  type        = string
  description = "Absolute external Kalshi private-key path."
}

variable "recorder_store_dir" {
  type        = string
  description = "Existing directory containing RecorderStore and its SQLite WAL/SHM siblings."
}

variable "recorder_health_dir" {
  type        = string
  description = "Existing parent directory for health.json."
}

variable "recorder_control_dir" {
  type        = string
  description = "Existing parent directory for recorder-control.json."
}

variable "recorder_pid_dir" {
  type        = string
  description = "Existing parent directory for recorder.pid."
}

variable "recorder_archive_dir" {
  type        = string
  description = "Existing mutable WebSocket archive directory."
}

variable "recorder_archive_manifest_dir" {
  type        = string
  description = "Existing parent directory for ws_archive_manifest.sqlite3."
}

variable "recorder_retention_state_dir" {
  type        = string
  description = "Existing parent directory for adaptive-retention.sqlite3."
}

variable "recorder_retention_status_dir" {
  type        = string
  description = "Existing parent directory for adaptive-retention.json."
}

job "live15-recorder-identity-preflight" {
  datacenters = ["dc1"]
  type        = "batch"

  group "identity-contract" {
    task "verify" {
      driver = "raw_exec"

      config {
        command = var.recorder_runtime_python
        args = [
          "-I",
          "-c",
          <<-PYTHON
          import sys, uuid
          from pathlib import Path

          runtime = Path(sys.executable)
          app = Path(r'${var.recorder_app_root}')
          if not runtime.is_file():
              raise RuntimeError("protected Recorder runtime is not a file")
          if not app.is_dir() or not (app / "src").is_dir():
              raise RuntimeError("immutable Recorder app root is missing")
          sys.path.insert(0, str(app / "src"))
          import live15_quant
          module_path = Path(live15_quant.__file__).resolve()
          if app.resolve() not in module_path.parents:
              raise RuntimeError("Recorder import escaped immutable app root")

          credential_paths = (
              Path(r'${var.kalshi_api_key_id_path}'),
              Path(r'${var.kalshi_private_key_path}'),
          )
          for path in credential_paths:
              with path.open("rb") as handle:
                  handle.read(1)

          mutable_dirs = (
              Path(r'${var.recorder_work_dir}'),
              Path(r'${var.recorder_store_dir}'),
              Path(r'${var.recorder_health_dir}'),
              Path(r'${var.recorder_control_dir}'),
              Path(r'${var.recorder_pid_dir}'),
              Path(r'${var.recorder_archive_dir}'),
              Path(r'${var.recorder_archive_manifest_dir}'),
              Path(r'${var.recorder_retention_state_dir}'),
              Path(r'${var.recorder_retention_status_dir}'),
          )
          for directory in mutable_dirs:
              if not directory.is_dir():
                  raise RuntimeError(f"required mutable directory is missing: {directory}")
              probe = directory / f".nomad-recorder-identity-probe-{uuid.uuid4().hex}"
              probe.write_bytes(b"identity-preflight")
              probe.unlink()
              if probe.exists():
                  raise RuntimeError(f"probe cleanup failed: {probe}")

          print("LIVE15_RECORDER_LOCAL_SERVICE_IDENTITY_PREFLIGHT_PASS")
          PYTHON
        ]
      }

      restart {
        attempts = 0
        mode     = "fail"
      }
    }
  }
}
