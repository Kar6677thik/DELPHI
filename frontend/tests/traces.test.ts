import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { TRACE_BATCH_MAX, TRACE_FLUSH_MS, createTraceQueue } from '../src/api/traces';

const CASE = '01930000-0000-7000-8000-000000000001';
const RECORD = '01930000-0000-7000-8000-0000000000e1';
const AT = new Date('2026-11-23T10:00:00Z');

beforeEach(() => {
  vi.useFakeTimers();
});
afterEach(() => {
  vi.useRealTimers();
});

describe('the trace queue (docs/09 §5)', () => {
  it('sends one batch per 5 s window, not one request per event', () => {
    const send = vi.fn(() => Promise.resolve());
    const queue = createTraceQueue(CASE, send, () => AT);
    queue.track('CASE_VIEWED');
    queue.track('EVIDENCE_VIEWED', RECORD);
    queue.track('WCMM_VIEWED');
    expect(send).not.toHaveBeenCalled();
    vi.advanceTimersByTime(TRACE_FLUSH_MS);
    expect(send).toHaveBeenCalledTimes(1);
    expect(send).toHaveBeenCalledWith(CASE, [
      { type: 'CASE_VIEWED', at: AT.toISOString() },
      {
        type: 'EVIDENCE_VIEWED',
        at: AT.toISOString(),
        payload: { evidence_record_id: RECORD },
      },
      { type: 'WCMM_VIEWED', at: AT.toISOString() },
    ]);
  });

  it('counts a view once per page, and every citation opened', () => {
    const send = vi.fn(() => Promise.resolve());
    const queue = createTraceQueue(CASE, send, () => AT);
    queue.track('CASE_VIEWED');
    queue.track('CASE_VIEWED');
    queue.track('EVIDENCE_VIEWED', RECORD);
    queue.track('EVIDENCE_VIEWED', RECORD);
    queue.track('NARRATIVE_EXPANDED', RECORD);
    queue.track('NARRATIVE_EXPANDED', RECORD);
    queue.flush();
    const [[, events]] = send.mock.calls as unknown as [[string, { type: string }[]]];
    expect(events.map((e) => e.type)).toEqual([
      'CASE_VIEWED',
      'EVIDENCE_VIEWED',
      'NARRATIVE_EXPANDED',
      'NARRATIVE_EXPANDED',
    ]);
  });

  it('flushes on demand, then starts a new window', () => {
    const send = vi.fn(() => Promise.resolve());
    const queue = createTraceQueue(CASE, send, () => AT);
    queue.track('CASE_VIEWED');
    queue.flush();
    queue.flush(); // nothing left: no empty request
    expect(send).toHaveBeenCalledTimes(1);
    queue.track('WCMM_VIEWED');
    vi.advanceTimersByTime(TRACE_FLUSH_MS);
    expect(send).toHaveBeenCalledTimes(2);
  });

  it("splits a long queue at the server's batch limit", () => {
    const send = vi.fn(() => Promise.resolve());
    const queue = createTraceQueue(CASE, send, () => AT);
    for (let i = 0; i < TRACE_BATCH_MAX + 5; i += 1) queue.track('NARRATIVE_EXPANDED', RECORD);
    queue.flush();
    const sizes = (send.mock.calls as unknown as [string, unknown[]][]).map(([, e]) => e.length);
    expect(sizes).toEqual([TRACE_BATCH_MAX, 5]);
  });

  it('drops a failed batch without throwing: analytics never block the console', async () => {
    const send = vi.fn(() => Promise.reject(new Error('offline')));
    const queue = createTraceQueue(CASE, send, () => AT);
    queue.track('CASE_VIEWED');
    expect(() => {
      queue.flush();
    }).not.toThrow();
    await vi.runAllTimersAsync();
    expect(send).toHaveBeenCalledTimes(1);
  });
});
