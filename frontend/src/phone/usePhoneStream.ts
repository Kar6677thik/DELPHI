import { useEffect, useRef, useState } from 'react';

import { PHONE_EVENT_TYPES, type PhoneEvent, phoneStreamUrl } from '../api/phone';
import { type StreamStatus } from '../lib/useEventStream';

/**
 * The customer's channel (docs/08 §4): one EventSource per persona, delivering the probe to
 * show, the mock SMS, a wrong-OTP result and the decision. The browser reconnects a dropped
 * stream by itself; a closed one (an HTTP error) is reopened after a short wait.
 */
export function usePhoneStream(
  customerId: string | null,
  onEvent: (event: PhoneEvent) => void,
): StreamStatus {
  const [status, setStatus] = useState<StreamStatus>('connecting');
  const handler = useRef(onEvent);
  useEffect(() => {
    handler.current = onEvent;
  }, [onEvent]);

  useEffect(() => {
    if (customerId === null) return undefined;
    let source: EventSource | null = null;
    let retry: ReturnType<typeof setTimeout> | undefined;
    let stopped = false;
    const open = () => {
      source = new EventSource(phoneStreamUrl(customerId));
      source.onopen = () => {
        setStatus('connected');
      };
      source.onerror = () => {
        if (source?.readyState === EventSource.CLOSED && !stopped) {
          setStatus('reconnecting');
          retry = setTimeout(open, RETRY_MS);
        } else {
          setStatus('reconnecting');
        }
      };
      for (const type of PHONE_EVENT_TYPES) {
        source.addEventListener(type, (message: MessageEvent<string>) => {
          try {
            const data: unknown = JSON.parse(message.data);
            handler.current({ type, data } as PhoneEvent);
          } catch {
            // A frame that is not JSON is not ours to show; the session view still updates.
          }
        });
      }
    };
    open();
    return () => {
      stopped = true;
      clearTimeout(retry);
      source?.close();
    };
  }, [customerId]);

  return status;
}

const RETRY_MS = 2_000;
