# Matt Pocock skills installation record

- Upstream: `mattpocock/skills`
- Revision: `6654f6b60cd9d5be8b54c6fafe44346dabeb3b76`
- License: MIT
- Intended official Codex command: `npx skills@latest add mattpocock/skills`

The host had no system `npx`. The bundled package runner reached the same
installer but its current package failed before installation because it could not
resolve its public `yaml` dependency. This worktree therefore pins and copies
the requested upstream files and templates from that exact repository revision.

Standard skill names are upstream files. Existing LIVE15 adaptations are retained
unchanged as `live15-diagnosing-bugs`, `live15-grill-with-docs`, and
`live15-tdd`; use them when LIVE15 safety wording is more specific than the
generic upstream workflow.
