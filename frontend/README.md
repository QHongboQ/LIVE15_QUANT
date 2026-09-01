# LIVE15 Quant frontend

Phase 1 greenfield shell built on the official upstream stack:

- React 18.3.1
- React Admin 5.15.1
- Material UI 7.3.5
- Vite 8.0.16
- TypeScript 5.9.3

The shell is read-only and consumes the existing FastAPI projections through
`src/api.ts`. Run `pnpm install`, then `pnpm dev` from this directory. The
backend is expected at the same origin in development or behind the deployed
origin in production; `VITE_API_BASE_URL` can override that base URL.

The packaged React terminal is the sole ControlCenter web owner. Rollback, if
needed after a deployed cutover, uses the prior immutable application release
rather than a source-level legacy shell.
