# FLOW-005B — Model Upstream Pins

This is a provenance manifest only.  No upstream repository is vendored, imported at runtime,
or authorized for training by this milestone.  Every reference is pinned to a full commit SHA;
floating branches and tags are not accepted.

| Reference | Role | Pinned commit | License evidence | Use boundary |
|---|---|---|---|---|
| [Time-Series-Library](https://github.com/thuml/Time-Series-Library) | Path Expert | `4e938a1767106324dd753b2a44832bf870a0252e` | MIT `LICENSE` | narrow offline adapter; TimeXer/PatchTST/iTransformer/TimeMixer/TimesNet/DLinear remain research references |
| [TLOB](https://github.com/LeonardoBerti00/TLOB) | Microstructure | `f1c0af4d81067978914361766db0457a7d8b6a46` | MIT `LICENSE` | bounded snapshot research adapter; no live/runtime wiring |
| [Qlib](https://github.com/microsoft/qlib) | Research orchestration | `79633dd9506ea689e5400dea0197717b5b3d74b7` | MIT `LICENSE` | manifest/fold-plan vocabulary only; not a dependency |
| [EarnHFT](https://github.com/TradeMaster-NTU/EarnHFT) | Hierarchical architecture | `0e1e11a6d9aff70efb1807baa3416429568deb31` | `NO_LICENSE_FILE_FOUND_AT_PIN` | architecture/provenance only; review license before any use or distribution |

The EarnHFT pin is intentionally retained as architecture-only with no license assumption.  No
code from that repository is included in LIVE15.  The exact revisions and license observations
were captured from the official repositories during FLOW-005B-PREP and are recorded in
`docs/model_upstream_pins.json`.
