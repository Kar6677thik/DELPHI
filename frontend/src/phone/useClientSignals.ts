import { useCallback, useEffect, useRef } from 'react';

import type { ClientSignals } from '../api/phone';

/**
 * What a probe screen measures while it is up (docs/08 §4 `client_signals`): when it was shown,
 * how often the customer left the app (coached victims switch to the caller's chat), and how
 * often they pasted (a code read out by a caller is typed; one pasted from another app is not).
 * The server checks the times against its own clock (400 INVALID_CLIENT_SIGNALS).
 */
export function useClientSignals() {
  const shownAt = useRef(0);
  const hidden = useRef(0);
  const pastes = useRef(0);

  useEffect(() => {
    shownAt.current = Date.now();
    const onVisibility = () => {
      if (document.visibilityState === 'hidden') hidden.current += 1;
    };
    document.addEventListener('visibilitychange', onVisibility);
    return () => {
      document.removeEventListener('visibilitychange', onVisibility);
    };
  }, []);

  const onPaste = useCallback(() => {
    pastes.current += 1;
  }, []);

  const stamp = useCallback(
    (): ClientSignals => ({
      shown_at_ms: shownAt.current,
      answered_at_ms: Date.now(),
      visibility_hidden_count: hidden.current,
      paste_count: pastes.current,
    }),
    [],
  );

  return { onPaste, stamp };
}
