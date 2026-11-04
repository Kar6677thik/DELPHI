import { act, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { SseIndicator } from '../src/components/SseIndicator';
import { SSE_RETRY_BASE_MS, SSE_RETRY_MAX_MS } from '../src/lib/config';
import { useEventStream } from '../src/lib/useEventStream';
import { FakeEventSource } from './fakeEventSource';

const URL = '/api/v1/events/stream?topics=cases';

function Probe() {
  return <SseIndicator status={useEventStream(URL)} />;
}

describe('useEventStream + SseIndicator (docs/18 §4)', () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });
  afterEach(() => {
    vi.useRealTimers();
  });

  it('starts connecting and shows connected once open', () => {
    render(<Probe />);
    expect(screen.getByRole('status')).toHaveTextContent('Live updates: Connecting…');
    expect(FakeEventSource.latest().url).toBe(URL);
    act(() => {
      FakeEventSource.latest().open();
    });
    expect(screen.getByRole('status')).toHaveTextContent('Connected');
  });

  it('shows reconnecting while the browser retries a dropped stream', () => {
    render(<Probe />);
    act(() => {
      FakeEventSource.latest().open();
      FakeEventSource.latest().fail({ retrying: true });
    });
    expect(screen.getByRole('status')).toHaveTextContent('Reconnecting…');
    expect(FakeEventSource.instances).toHaveLength(1);
  });

  it('reconnects with capped exponential backoff when the browser gives up', () => {
    render(<Probe />);
    const delays: number[] = [];
    for (let attempt = 0; attempt < 7; attempt += 1) {
      const before = FakeEventSource.instances.length;
      act(() => {
        FakeEventSource.latest().fail({ retrying: false });
      });
      const expected = Math.min(SSE_RETRY_BASE_MS * 2 ** attempt, SSE_RETRY_MAX_MS);
      act(() => {
        vi.advanceTimersByTime(expected - 1);
      });
      expect(FakeEventSource.instances).toHaveLength(before);
      act(() => {
        vi.advanceTimersByTime(1);
      });
      expect(FakeEventSource.instances).toHaveLength(before + 1);
      delays.push(expected);
    }
    expect(delays.at(-1)).toBe(SSE_RETRY_MAX_MS);
    expect(FakeEventSource.instances.slice(0, -1).every((s) => s.closed)).toBe(true);
  });

  it('resets the backoff after a successful open', () => {
    render(<Probe />);
    act(() => {
      FakeEventSource.latest().fail({ retrying: false });
      vi.advanceTimersByTime(SSE_RETRY_BASE_MS);
      FakeEventSource.latest().fail({ retrying: false });
      vi.advanceTimersByTime(SSE_RETRY_BASE_MS * 2);
      FakeEventSource.latest().open();
      FakeEventSource.latest().fail({ retrying: false });
      vi.advanceTimersByTime(SSE_RETRY_BASE_MS);
    });
    expect(FakeEventSource.instances).toHaveLength(4);
  });

  it('closes the stream and stops retrying on unmount', () => {
    const { unmount } = render(<Probe />);
    act(() => {
      FakeEventSource.latest().fail({ retrying: false });
    });
    unmount();
    act(() => {
      vi.advanceTimersByTime(SSE_RETRY_MAX_MS);
    });
    expect(FakeEventSource.instances).toHaveLength(1);
    expect(FakeEventSource.latest().closed).toBe(true);
  });
});
