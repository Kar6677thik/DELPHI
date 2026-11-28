import { describe, expect, it } from 'vitest';

import { kbDisplay } from '../src/api/guardian';

/**
 * The api types the KB content and the planning candidates as open objects (#198), so the
 * frontend reads them with field checks. Malformed input must degrade, never throw or leak
 * through as the wrong type.
 */
describe('kbDisplay (docs/07 §4)', () => {
  const UNEXPLAINED = { UNEXPLAINED: { name: 'Unexplained', color: '#94A3B8' } };

  it('reads names, colours and the GUARDIAN_PAYMENT decisions', () => {
    const display = kbDisplay({
      hypotheses: { ATO: { display_name: 'Account takeover', color: '#7C3AED' } },
      decision_profiles: { GUARDIAN_PAYMENT: { decisions: ['RELEASE', 'DECLINE'] } },
    });
    expect(display.styles).toEqual({
      ...UNEXPLAINED,
      ATO: { name: 'Account takeover', color: '#7C3AED' },
    });
    expect(display.decisions).toEqual(['RELEASE', 'DECLINE']);
  });

  it.each([null, undefined, 'kb', 42, [], ['hypotheses']])(
    'gives only the implicit hypothesis and no decisions for content %j',
    (content) => {
      expect(kbDisplay(content)).toEqual({ styles: UNEXPLAINED, decisions: [], outcomes: {} });
    },
  );

  it('falls back field by field when entries are malformed', () => {
    const display = kbDisplay({
      hypotheses: {
        GENUINE: { display_name: 7, color: null }, // wrong types: the key and the fallback grey
        MULE: 'not an object',
        ATO: { color: '#7C3AED' }, // no name: the key
      },
      decision_profiles: { GUARDIAN_PAYMENT: { decisions: ['RELEASE', 3, null, 'DECLINE'] } },
    });
    expect(display.styles).toEqual({
      ...UNEXPLAINED,
      GENUINE: { name: 'GENUINE', color: '#94A3B8' },
      MULE: { name: 'MULE', color: '#94A3B8' },
      ATO: { name: 'ATO', color: '#7C3AED' },
    });
    expect(display.decisions).toEqual(['RELEASE', 'DECLINE']);
  });

  it("reads each source's outcomes, skipping malformed ones", () => {
    const display = kbDisplay({
      sources: {
        CUSTOMER_OUTREACH_CALL: { outcomes: ['CONFIRMS_COERCION', 4, 'UNREACHABLE'] },
        INBOUND_FAN_IN: { outcomes: 'HIGH' },
        BROKEN: null,
      },
    });
    expect(display.outcomes).toEqual({
      CUSTOMER_OUTREACH_CALL: ['CONFIRMS_COERCION', 'UNREACHABLE'],
      INBOUND_FAN_IN: [],
      BROKEN: [],
    });
  });

  it('has no decisions when the GUARDIAN_PAYMENT profile is missing or malformed', () => {
    expect(kbDisplay({ decision_profiles: {} }).decisions).toEqual([]);
    expect(kbDisplay({ decision_profiles: { GUARDIAN_PAYMENT: 'x' } }).decisions).toEqual([]);
    expect(
      kbDisplay({ decision_profiles: { GUARDIAN_PAYMENT: { decisions: 'RELEASE' } } }).decisions,
    ).toEqual([]);
  });
});
