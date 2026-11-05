import { useEffect, useState } from 'react';

import { SSE_RETRY_BASE_MS, SSE_RETRY_MAX_MS } from './config';

export type StreamStatus = 'connecting' | 'connected' | 'reconnecting';

/**
 * Keeps an EventSource open and reports its state for the header (docs/18 §4).
 *
 * The browser reconnects a dropped stream by itself, but gives up for good when the server
 * answers with an HTTP error (readyState CLOSED); then this hook reconnects with backoff.
 */
export function useEventStream(url: string): StreamStatus {
  const [status, setStatus] = useState<StreamStatus>('connecting');

  useEffect(() => {
    let source: EventSource | null = null;
    let retry: ReturnType<typeof setTimeout> | undefined;
    let failures = 0;

    const connect = () => {
      const current = new EventSource(url);
      source = current;
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
