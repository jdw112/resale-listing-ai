# Acme Resale Frontend

React + TypeScript SPA (Vite), talking to the FastAPI backend (`resale_listing_ai/api.py`) over session-cookie auth.

## Development

```bash
npm install
npm run dev
```

The dev server proxies `/v1/*` requests to `http://localhost:8000` (see `vite.config.ts`) — run the backend separately (`uvicorn resale_listing_ai.api:app --reload` from the repo root) for the proxy to have something to talk to.

## Testing

```bash
npm test          # Vitest + React Testing Library
npx tsc -b --noEmit  # type check
npm run build      # production build
```
