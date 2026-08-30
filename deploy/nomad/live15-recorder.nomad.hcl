# LIVE15 Recorder service job for a direct reversible cutover.
# Declarative only; this PR does not submit or mutate Production.

variable "recorder_runtime_python" {
  type        = string
  description = "Absolute protected CPython venv executable provisioned for Recorder."
}

variable "recorder_app_root" {
  type        = string
  description = "Absolute immutable release app root (releases/<release-id>/app)."
}

variable "recorder_work_dir" {
  type        = string
  description = "Absolute existing mutable Recorder working/data root."
}

variable "recorder_data_path" {
  type        = string
  description = "Absolute RecorderStore path; remains outside the immutable app root."
}

variable "recorder_health_path" {
  type        = string
  description = "Absolute atomic Recorder health heartbeat path."
}

variable "recorder_control_path" {
  type        = string
  description = "Absolute Recorder control-file path."
}

variable "recorder_pid_path" {
  type        = string
  description = "Absolute Recorder PID lease path."
}

variable "kalshi_api_key_id_path" {
  type        = string
  description = "Existing absolute external Kalshi API-key identifier path; content is never read here."
}

variable "kalshi_private_key_path" {
  type        = string
  description = "Existing absolute external Kalshi private-key path; content is never read here."
}

variable "pyth_api_key_path" {
  type        = string
  description = "Existing absolute external Pyth API-key path; content is never logged."
}

variable "release_id" {
  type        = string
  description = "Verified immutable release identity supplied at the future cutover gate."
}

variable "release_git_sha" {
  type        = string
  description = "40-character protected source SHA for the verified release."
}

variable "release_manifest_sha256" {
  type        = string
  description = "SHA-256 of the verified release manifest."
}

variable "artifact_manifest_sha256" {
  type        = string
  description = "SHA-256 of the verified release artifact inventory."
}

variable "requirements_lock_sha256" {
  type        = string
  description = "SHA-256 of the release requirements.lock."
}

variable "runtime_python_sha256" {
  type        = string
  description = "SHA-256 of the protected runtime python.exe."
}

job "live15-recorder" {
  # dc1 is the existing Nomad agent's declared datacenter; this is placement,
  # not a new service-discovery dependency.
  datacenters = ["dc1"]
  # A Recorder is a long-lived service workload; Nomad owns its allocation
  # lifecycle while the existing entrypoint owns domain behavior.
  type        = "service"

  # Native task-state deployment health is the only available health signal;
  # no Recorder HTTP/TCP bridge or check_restart is introduced.
  update {
    health_check = "task_states"
    auto_revert  = true
  }

  group "recorder" {
    # Three bounded local restarts mirror the existing finite WinSW failure
    # budget. mode=fail stops this allocation after the budget is exhausted.
    restart {
      attempts = 3
      interval = "5m"
      delay    = "15s"
      mode     = "fail"
    }

    # A failed allocation must not become an infinite reschedule storm.
    reschedule {
      attempts  = 0
      unlimited = false
    }

    task "recorder" {
      driver       = "raw_exec"
      kill_timeout = "15s"

      meta {
        release_id                = var.release_id
        release_git_sha            = var.release_git_sha
        release_manifest_sha256   = var.release_manifest_sha256
        artifact_manifest_sha256  = var.artifact_manifest_sha256
        requirements_lock_sha256 = var.requirements_lock_sha256
        runtime_python_sha256     = var.runtime_python_sha256
      }

      config {
        command  = var.recorder_runtime_python
        work_dir = var.recorder_work_dir
        args = [
          "-I",
          "-c",
          "import sys; from pathlib import Path; app = Path(r'${var.recorder_app_root}'); sys.path.insert(0, str(app / 'src')); from live15_quant.cli import recorder_main; recorder_main()",
        ]
      }

      env {
        PYTHONDONTWRITEBYTECODE                    = "1"
        PYTHONUNBUFFERED                           = "1"
        PYTHONUTF8                                 = "1"
        LIVE15_PYTH_HERMES_BASE_URL                = "https://pyth.dourolabs.app/hermes"
        LIVE15_ENABLE_PYTH_UNDERLYING              = "true"
        LIVE15_PYTH_API_KEY_PATH                   = var.pyth_api_key_path
        LIVE15_ENABLE_KALSHI_PRODUCTION_WEBSOCKET  = "true"
        LIVE15_KALSHI_RECORDER_PROVIDER            = "sdk"
        LIVE15_KALSHI_PRODUCTION_API_KEY_ID_PATH   = var.kalshi_api_key_id_path
        LIVE15_KALSHI_PRODUCTION_PRIVATE_KEY_PATH  = var.kalshi_private_key_path
        LIVE15_RECORDER_DATA_PATH                  = var.recorder_data_path
        LIVE15_RECORDER_HEALTH_PATH                = var.recorder_health_path
        LIVE15_RECORDER_CONTROL_PATH               = var.recorder_control_path
        LIVE15_RECORDER_PID_PATH                   = var.recorder_pid_path
      }
    }
  }
}
