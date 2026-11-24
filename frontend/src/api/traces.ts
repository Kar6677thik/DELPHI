import { createContext, useContext, useEffect, useMemo, useRef } from 'react';

import { apiFetch } from './client';
import type { components } from './schema';

/**
 * The console's view events (docs/09 §5, FR-NAV-08): queued in the browser and sent in batches to
 * `POST /cases/{id}/trace-events`, at most one request per 5 s. The server records the actions
 * (opening, fetching, deciding, reading the DD pack) itself; the console sends views only.
 * Analytics must never get in the investigator's way, so a failed send is dropped silently.
 */

type TraceEventIn = components['schemas']['TraceEventIn'];

/** The events the console may send (docs/16); the server refuses any other type. */
export type ViewTraceType =
  'CASE_VIEWED' | 'EVIDENCE_VIEWED' | 'NARRATIVE_EXPANDED' | 'WCMM_VIEWED';

/** docs/09 §5: at most one request per 5 s. */
export const TRACE_FLUSH_MS = 5_000;
/** The server's limit on one batch (`MAX_TRACE_BATCH`, docs/16). */
export const TRACE_BATCH_MAX = 100;

export type SendBatch = (caseId: string, events: TraceEventIn[]) => Promise<unknown>;

export const sendBatch: SendBatch = (caseId, events) =>
  apiFetch(`/cases/${encodeURIComponent(caseId)}/trace-events`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ events }),
  });

export interface TraceQueue {
  /** Queue one event. A view counts once per page; every citation opened counts. */
  track: (type: ViewTraceType, evidenceRecordId?: string) => void;
  /** Send what is queued now (on leaving the page). */
  flush: () => void;
}

/** A queue for one case that sends at most one batch per `TRACE_FLUSH_MS`. */
export function createTraceQueue(
  caseId: string,
  send: SendBatch = sendBatch,
  now: () => Date = () => new Date(),
): TraceQueue {
  let queued: TraceEventIn[] = [];
  let timer: ReturnType<typeof setTimeout> | null = null;
  const seen = new Set<string>();

  const flush = () => {
    if (timer !== null) {
      clearTimeout(timer);
      timer = null;
    }
    while (queued.length > 0) {
      const batch = queued.slice(0, TRACE_BATCH_MAX);
      queued = queued.slice(TRACE_BATCH_MAX);
      void send(caseId, batch).catch(() => undefined); // analytics never block the console
    }
  };

  const track = (type: ViewTraceType, evidenceRecordId?: string) => {
    const key = `${type}:${evidenceRecordId ?? ''}`;
    if (type !== 'NARRATIVE_EXPANDED' && seen.has(key)) return; // a view counts once per page
    seen.add(key);
    queued.push({
      type,
      at: now().toISOString(),
      ...(evidenceRecordId ? { payload: { evidence_record_id: evidenceRecordId } } : {}),
    });
    timer ??= setTimeout(flush, TRACE_FLUSH_MS);
  };

  return { track, flush };
}

/**
 * The console's queue for its case, flushed when the console unmounts. The flush waits a tick and
 * is cancelled if the console mounts again at once (React's development double mount), so only a
 * real departure sends early.
 */
export function useTraceQueue(caseId: string): TraceQueue {
  const queue = useMemo(() => createTraceQueue(caseId), [caseId]);
  const leaving = useRef<ReturnType<typeof setTimeout> | null>(null);
  useEffect(() => {
    if (leaving.current !== null) clearTimeout(leaving.current);
    return () => {
      leaving.current = setTimeout(queue.flush, 0);
    };
  }, [queue]);
  return queue;
}

type Track = TraceQueue['track'];
const noTrack: Track = () => undefined;

/** Lets the console's components record views without threading a callback through props. */
export const TraceContext = createContext<Track>(noTrack);

export function useTrack(): Track {
  return useContext(TraceContext);
}
