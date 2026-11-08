/** The SSE stream and the topics the shell subscribes to (docs/16 §3). */
export const EVENT_STREAM_URL = '/api/v1/events/stream?topics=guardian,cases,chase,jobs,scout,kb';

/**
 * docs/16 §3's event types. Frames are named (`event: <type>`), so `onmessage` never sees them;
 * the stream hook listens to each to know the last event id it received.
 */
export const EVENT_TYPES = [
  'guardian.session.updated',
  'guardian.session.decided',
  'case.opened',
  'case.updated',
  'evidence.narrative_ready',
  'campaign.updated',
  'chase.updated',
  'chase.lien.updated',
  'job.updated',
  'scout.cluster.created',
  'kb.version.changed',
  'sim.transfer',
] as const;

/** Reconnect backoff after the browser gives up on the stream (doubling, capped). */
export const SSE_RETRY_BASE_MS = 1_000;
export const SSE_RETRY_MAX_MS = 30_000;

/** Server errors (5xx) and network failures are retried this many times; 4xx never are. */
export const QUERY_MAX_RETRIES = 2;
