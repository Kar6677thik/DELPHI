/** The SSE stream and the topics the shell subscribes to (docs/16 §3). */
export const EVENT_STREAM_URL = '/api/v1/events/stream?topics=guardian,cases,chase,jobs,scout,kb';

/** Reconnect backoff after the browser gives up on the stream (doubling, capped). */
export const SSE_RETRY_BASE_MS = 1_000;
export const SSE_RETRY_MAX_MS = 30_000;

/** Server errors (5xx) and network failures are retried this many times; 4xx never are. */
export const QUERY_MAX_RETRIES = 2;
