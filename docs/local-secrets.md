# Local secret references

LIVE15 keeps secret values outside Git and exposes only a path/reference to provider code.
`.secrets/` is ignored and must never be committed, included in baselines/model artifacts,
shown through Control Center, or written to logs.

Current Windows development flow:

```text
LIVE15 configuration
  -> `LIVE15_PYTH_API_KEY_PATH` reference
  -> `D:\LIVE15_QUANT\.secrets\pyth-api-key.txt`
  -> Pyth provider
```

Resolution order is:

1. an explicit configured path;
2. the project-local `.secrets/<name>` path;
3. the legacy user-profile path when it is readable.

The one migration exception is the known legacy Pyth default: when the local replacement
exists, it supersedes a stale inherited reference to that legacy path. Other explicit paths
remain authoritative.

Future deployment adapters are intentionally deferred (`DEFERRED`): WSL/Linux can provide a
mounted file or environment reference, and cloud/container deployments can implement a
`SecretProvider` for AWS Secrets Manager, Azure Key Vault, Google Secret Manager, or a mounted
Kubernetes/Docker secret. Recorder, model, and business code should continue to consume only a
resolved reference, never a backend-specific secret lookup.
