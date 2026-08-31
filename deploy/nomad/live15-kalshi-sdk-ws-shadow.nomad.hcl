# LIVE15 kalshi-sdk WebSocket parity shadow job.
# Declarative only: committing this file does not register, start, restart, or mutate Production.
# The job remains ON_DEMAND until a separately authorized operator submits it.

variable "shadow_runtime_python" {
  type        = string
  description = "Absolute canonical protected LIVE15 Production Python executable."
}

variable "shadow_app_root" {
  type        = string
  description = "Absolute immutable release app root (releases/<release-id>/app)."
}

variable "shadow_work_dir" {
  type        = string
  description = "Absolute existing mutable LIVE15 working root containing runtime/, data/, and logs/."
}

variable "recorder_health_path" {
  type        = string
  description = "Absolute existing Recorder health projection consumed read-only for the current market universe."
}

variable "kalshi_api_key_id_path" {
  type        = string
  description = "Existing absolute external Kalshi API-key identifier path; content is never embedded here."
}

variable "kalshi_private_key_path" {
  type        = string
  description = "Existing absolute external Kalshi private-key path; content is never embedded here."
}

variable "release_id" {
  type        = string
  description = "Verified immutable release identity supplied at a future activation gate."
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
  description = "SHA-256 of the canonical protected runtime python.exe."
}

job "live15-kalshi-sdk-ws-shadow" {
  datacenters = ["dc1"]
  type        = "service"

  # Nomad shutdown maps to CPython's Windows-only SIGBREAK handler.
  constraint {
    attribute = "${attr.kernel.name}"
    value     = "windows"
  }

  update {
    health_check = "task_states"
    auto_revert  = true
  }

  group "kalshi-sdk-ws-shadow" {
    # Nomad replaces RuntimeSupervisor's generic child restart/backoff responsibility.
    # A bounded failure budget prevents an infinite local restart loop.
    restart {
      attempts = 3
      interval = "5m"
      delay    = "15s"
      mode     = "fail"
    }

    # Do not turn a failed read-only parity shadow into an unbounded reschedule storm.
    reschedule {
      attempts  = 0
      unlimited = false
    }

    task "kalshi-sdk-ws-shadow" {
      driver       = "raw_exec"
      kill_timeout = "30s"

      meta {
        release_id               = var.release_id
        release_git_sha          = var.release_git_sha
        release_manifest_sha256  = var.release_manifest_sha256
        artifact_manifest_sha256 = var.artifact_manifest_sha256
        requirements_lock_sha256 = var.requirements_lock_sha256
        runtime_python_sha256    = var.runtime_python_sha256
      }

      config {
        command  = var.shadow_runtime_python
        work_dir = var.shadow_work_dir
        args = [
          "-I",
          "-c",
          "import sys; from pathlib import Path; app = Path(r'${var.shadow_app_root}'); sys.path.insert(0, str(app / 'src')); from live15_quant.managed_kalshi_sdk_shadow import main; main()",
        ]
      }

      # Preserve the legacy production_runtime_environment boundary explicitly.
      # Empty values override any inherited Demo/endpoint variables on the Nomad client.
      env {
        PYTHONDONTWRITEBYTECODE                          = "1"
        PYTHONUNBUFFERED                                 = "1"
        PYTHONUTF8                                       = "1"
        KALSHI_DEMO                                      = "false"
        KALSHI_BASE_URL                                  = ""
        KALSHI_WS_BASE_URL                               = ""
        LIVE15_KALSHI_DEMO_API_KEY_ID                    = ""
        LIVE15_KALSHI_DEMO_API_KEY_ID_FILE               = ""
        LIVE15_KALSHI_DEMO_PRIVATE_KEY_PATH              = ""
        LIVE15_KALSHI_RUNTIME_ENVIRONMENT                = "PRODUCTION"
        LIVE15_ENABLE_KALSHI_PRODUCTION_WEBSOCKET        = "true"
        LIVE15_KALSHI_SDK_SHADOW_LIFECYCLE_OWNER        = "nomad"
        LIVE15_KALSHI_PRODUCTION_API_KEY_ID_PATH         = var.kalshi_api_key_id_path
        LIVE15_KALSHI_PRODUCTION_PRIVATE_KEY_PATH        = var.kalshi_private_key_path
        LIVE15_RECORDER_HEALTH_PATH                      = var.recorder_health_path
      }
    }
  }
}
