# ADR-0010 — Public access via Cloudflare Tunnel, protected by app login

- **Status:** Accepted
- **Date:** 2026-10-31

## Context
Judges and teammates need a link. The home network should not expose ports.

## Decision
A remotely-managed Cloudflare Tunnel (a `cloudflared` Deployment in the namespace) publishes
`delphi.<domain>` → `delphi-api:8000`. Every page and API except `/health` and `/ready` requires
login. `/metrics` is on a separate port that is not routed. Cloudflare rate limiting protects
login.

## Alternatives considered
- LAN/Tailscale only: safest, but judges cannot open a link.
- Port forwarding + reverse proxy + Let's Encrypt: exposes the home IP and opens router ports.

## Consequences
- Positive: no inbound ports; TLS at the Cloudflare edge.
- Negative: depends on Cloudflare availability. Fallback: Tailscale access for the presenter.
