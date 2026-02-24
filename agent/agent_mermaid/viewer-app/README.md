# Mermaid Agent Viewer (SPA)

Separate Vite + React + React Flow codebase for the Agent Monitor viewer. It is served by the SOP MCP server at **`/app/viewer`** when this app is built.

## Build

```bash
npm install
npm run build
```

Output is written to `dist/`. The Python server (`sop_mcp_server`) serves this directory at `/app/viewer` when `viewer-app/dist` exists.

## Dev

```bash
npm run dev
```

For dev you must use the same base path when proxying to the backend: configure Vite proxy so `/api/*` goes to the server (e.g. `http://localhost:8000`).

## Routes

- `/app/viewer/` — Agents (list of mermaid-agents)
- `/app/viewer/sessions` — Sessions (live and past MCP sessions)
- `/app/viewer/session/:id` — Session detail: split view (traces + React Flow graph), node click opens side panel

API used: `GET /api/agents`, `GET /api/connections`, `GET /api/connections/:id`.
