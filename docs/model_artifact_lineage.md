# Model artifact lineage contract

Model binaries and generated datasets are runtime artifacts under `data/` and are intentionally
excluded from Git. A fresh clone must not silently invent or replace them. Before Paper/Shadow
startup, the external artifact store must provide a manifest for the selected artifact with at
least:

- model ID and promotion status;
- artifact path or registry reference and SHA-256 digest;
- dataset ID and deterministic build hash;
- feature schema and label schema versions;
- training code Git SHA;
- preprocessing/calibration configuration;
- validation-selection and final-test metrics;
- cost assumptions and creation timestamp.

The repository already writes and validates these fields in generated Dataset/Model Zoo
manifests (for example under `data/datasets/` and `data/models/`). This document is the tracked
reconstruction contract; it does not copy model weights or active runtime data into Git. A clone
without the external artifact manifests must fail closed as artifact-unavailable rather than
claiming a reproducible model runtime.
