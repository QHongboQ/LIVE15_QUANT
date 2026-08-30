job "live15-control-center-artifact-preflight" {
  datacenters = ["dc1"]
  type        = "batch"

  group "artifact-contract" {
    task "verify" {
      driver = "raw_exec"

      config {
        command = "C:\\Program Files\\LIVE15\\ControlCenterRuntime\\Scripts\\python.exe"
        args = [
          "-I",
          "-c",
          "import sys; from pathlib import Path; app = Path(r'C:\\Program Files\\LIVE15\\ControlCenterReleases\\releases\\live15-b1e1894c7666-c0b6557e6fc9\\app'); sys.path.insert(0, str(app / 'src')); import live15_quant; assert Path(live15_quant.__file__).resolve().is_relative_to(app.resolve()); print('LIVE15_CONTROL_CENTER_IMMUTABLE_ARTIFACT_PASS')",
        ]
      }

      restart {
        attempts = 0
        mode     = "fail"
      }

      resources {
        cpu    = 100
        memory = 256
      }
    }
  }
}
