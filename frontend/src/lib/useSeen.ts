import { type RefObject, useEffect } from 'react';

/** How much of an element must be on screen for it to count as seen. */
const SEEN_SHARE = 0.5;

/**
 * Calls `onSeen` once, the first time at least half of the element is on screen (a view event,
 * docs/09 §5). Without `IntersectionObserver` (old browsers, tests) the element counts as seen
 * when it is shown.
 */
export function useSeen(ref: RefObject<Element | null>, onSeen: () => void): void {
  useEffect(() => {
    const element = ref.current;
    if (!element) return;
    if (typeof IntersectionObserver === 'undefined') {
      onSeen();
      return;
    }
    const observer = new IntersectionObserver(
      (entries) => {
        if (entries.some((e) => e.isIntersecting)) {
          onSeen();
          observer.disconnect();
        }
      },
      { threshold: SEEN_SHARE },
    );
    observer.observe(element);
    return () => {
      observer.disconnect();
    };
  }, [ref, onSeen]);
}
