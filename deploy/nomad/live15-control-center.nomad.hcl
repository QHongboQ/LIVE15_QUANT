variable "kalshi_production_api_key_id_path" {
  type        = string
  description = "Existing absolute external API-key identifier path; never credential content."
}

variable "kalshi_production_private_key_path" {
  type        = string
  description = "Existing absolute external private-key path; never credential content."
}

job "live15-control-center" {
  datacenters = ["dc1"]
  type        = "service"

  update {
    max_parallel      = 1
    health_check      = "checks"
    min_healthy_time  = "10s"
    healthy_deadline  = "2m"
    progress_deadline = "4m"
    auto_revert       = true
    auto_promote      = false
    canary            = 0
  }

  group "control-center" {
    network {
      port "http" {
        static       = 8765
        to           = 8765
        host_network = "loopback"
      }
    }

    restart {
      attempts = 3
      interval = "15m"
      delay    = "2s"
      mode     = "delay"
    }

    task "control-center" {
      driver         = "raw_exec"
      shutdown_delay = "5s"
      kill_timeout   = "30s"

      meta {
        release_id               = "live15-b1e1894c7666-c0b6557e6fc9"
        git_sha                  = "b1e1894c7666e9763b3994cc8135ad0d7727698e"
        source_tree              = "c0b6557e6fc9b8c6e6875abbd2dc7b7b6c8a478d"
        release_manifest_sha256  = "420E1167FCC3F83EF0076ED197228A3C98ED46A76DCCBF539D48B5A020FB3596"
        artifact_manifest_sha256 = "175C468974EA8C52F57CB5F8261D00382C2ABC6FBAC5D24C0CAFAE9304F49D5F"
        requirements_lock_sha256 = "4521A9151C00797B004CD6AEB12A054DD5759BD211333D012736CED3E635A67E"
        runtime_python_sha256    = "72B29481593C5DA37C99248C82777FBFB56217EA7809B771BC760D0A9ECB179B"
      }

      config {
        command  = "C:\\Program Files\\LIVE15\\ControlCenterRuntime\\Scripts\\python.exe"
        work_dir = "D:\\LIVE15_QUANT"
        args = [
          "-I",
          "-c",
          "import sys; from pathlib import Path; app = Path(r'C:\\Program Files\\LIVE15\\ControlCenterReleases\\releases\\live15-b1e1894c7666-c0b6557e6fc9\\app'); sys.path.insert(0, str(app / 'src')); from live15_quant.control_center import main; main()",
          "--port",
          "8765",
        ]
      }

      env {
        LIVE15_KALSHI_PRODUCTION_API_KEY_ID_PATH = var.kalshi_production_api_key_id_path
        LIVE15_KALSHI_PRODUCTION_PRIVATE_KEY_PATH = var.kalshi_production_private_key_path
        PYTHONDONTWRITEBYTECODE                     = "1"
        PYTHONUNBUFFERED                            = "1"
        PYTHONUTF8                                  = "1"
      }

      resources {
        cpu    = 500
        memory = 512
      }

      service {
        name     = "live15-control-center"
        port     = "http"
        provider = "nomad"

        check {
          name     = "control-center-truthful-health"
          type     = "http"
          path     = "/api/health"
          interval = "5s"
          timeout  = "2s"

          check_restart {
            limit = 2
            grace = "15s"
          }
        }
      }
    }
  }
}
