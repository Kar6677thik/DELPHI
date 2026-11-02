# ADR-0008 — React + TypeScript SPA served by the API process

- **Status:** Accepted
- **Date:** 2026-10-31

## Context
We need an investigator console, a realistic mock phone, live updates, graph and chart views,
and demo polish. The owner chose React + TypeScript over Streamlit.

## Decision
Vite + React 18 + TypeScript strict, TanStack Query, Tailwind + Radix, Recharts, xyflow. FastAPI
serves the built assets from `/` (same origin, so no CORS and simple cookies). API types are
generated from OpenAPI.

## Alternatives considered
- Streamlit: fast, but poor for the mock phone, live updates and custom layouts.
- A separate nginx container for static files: an extra deployable for no benefit at this scale.

## Consequences
- Positive: one origin, one image. Typed contract end to end.
- Negative: a Node toolchain in the build (multi-stage Dockerfile).
