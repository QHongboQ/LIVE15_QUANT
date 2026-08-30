job "live15-control-center-shadow" {
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

  group "control-center-shadow" {
    network {
      port "http" {
        static       = 18081
        to           = 18081
        host_network = "loopback"
      }
    }

    restart {
      attempts = 3
      interval = "15m"
      delay    = "2s"
      mode     = "delay"
    }

    task "control-center-shadow" {
      driver = "raw_exec"
      shutdown_delay = "2s"

      meta {
        artifact_sha256 = "4D06F9641BA468D4C351190AB5F4E8D1D5F5BEB1463FFF85985190F46662127B"
        scope           = "non-production-read-only-shadow"
      }

      config {
        command = "C:\\Windows\\System32\\WindowsPowerShell\\v1.0\\powershell.exe"
        args = [
          "-NoLogo",
          "-NoProfile",
          "-ExecutionPolicy", "Bypass",
          "-File", "D:\\LIVE15_NOMAD_POC\\control-center-shadow\\artifact\\live15-control-center-shadow.ps1",
          "-Port", "18081",
          "-ExpectedSha256", "4D06F9641BA468D4C351190AB5F4E8D1D5F5BEB1463FFF85985190F46662127B",
          "-EvidenceLog", "D:\\LIVE15_NOMAD_POC\\control-center-shadow\\logs\\artifact-runtime.log"
        ]
      }

      resources {
        cpu    = 100
        memory = 64
      }

      service {
        name     = "live15-control-center-shadow"
        port     = "http"
        provider = "nomad"

        check {
          name     = "nomad-liveness"
          type     = "http"
          path     = "/_nomad/healthz"
          interval = "5s"
          timeout  = "2s"

          check_restart {
            limit = 2
            grace = "10s"
          }
        }
      }
    }
  }
}
