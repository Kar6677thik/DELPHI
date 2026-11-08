import { useEffect, useState } from 'react';

import { EVENT_TYPES, SSE_RETRY_BASE_MS, SSE_RETRY_MAX_MS } from './config';

export type StreamStatus = 'connecting' | 'connected' | 'reconnecting';

/** ``url`` resuming after event ``lastId`` (docs/16 §3: the query form of `Last-Event-ID`). */
export function resumeUrl(url: string, lastId: string | null): string {
  if (lastId === null) return url;
  const resumed = new URL(url, window.location.origin);
  resumed.searchParams.set('last_event_id', lastId);
  return resumed.pathname + resumed.search;
}

/**
 * Keeps an EventSource open and reports its state for the header (docs/18 §4).
 *
 * The browser reconnects a dropped stream by itself, sending `Last-Event-ID`, but gives up for
 * good when the server answers with an HTTP error (readyState CLOSED: a 401, or a 502 while the
 * api restarts). Then this hook reconnects with backoff, and since a new EventSource cannot set
 * that header, it passes the last event id it saw as `?last_event_id=`, so the server replays
 * what was missed (docs/16 §3).
 */
export function useEventStream(url: string): StreamStatus {
  const [status, setStatus] = useState<StreamStatus>('connecting');

  useEffect(() => {
    let source: EventSource | null = null;
    let retry: ReturnType<typeof setTimeout> | undefined;
    let failures = 0;
    let lastId: string | null = null;
    const remember = (event: MessageEvent) => {
      if (event.lastEventId) lastId = event.lastEventId;
    };

    const connect = () => {
      const current = new EventSource(resumeUrl(url, lastId));
      source = current;
      for (const type of EVENT_TYPES) current.addEventListener(type, remember);
      current.onopen = () => {
        failures = 0;
        setStatus('connected');
      };
      current.onerror = () => {
        setStatus('reconnecting');
        if (current.readyState !== EventSource.CLOSED) return;
        current.close();
        const delay = Math.min(SSE_RETRY_BASE_MS * 2 ** failures, SSE_RETRY_MAX_MS);
        failures += 1;
        retry = setTimeout(connect, delay);
      };
    };

    connect();
    return () => {
      clearTimeout(retry);
      source?.close();
    };
  }, [url]);

  return status;
}
