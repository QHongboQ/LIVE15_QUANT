job "live15-control-center-runtime-preflight" {
  datacenters = ["dc1"]
  type        = "batch"

  group "runtime-contract" {
    task "verify" {
      driver = "raw_exec"

      config {
        command = "C:\\Program Files\\LIVE15\\ControlCenterRuntime\\Scripts\\python.exe"
        args = [
          "-I",
          "-c",
          "import importlib.metadata as m; import kalshi, fastapi, numpy, uvicorn, xgboost; assert m.version('kalshi-sdk') == '12.0.0'; assert fastapi.__version__ == '0.141.1'; assert uvicorn.__version__ == '0.52.4'; assert numpy.__version__ == '2.5.2'; assert xgboost.__version__ == '3.4.1'; print('LIVE15_CONTROL_CENTER_LOCAL_SERVICE_RUNTIME_PASS')",
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
