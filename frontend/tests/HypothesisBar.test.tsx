import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';

import {
  HypothesisBar,
  HypothesisTimeline,
  LABEL_MIN_SHARE,
  segments,
  textColor,
} from '../src/components/HypothesisBar';

const STYLES = {
  GENUINE: { name: 'Genuine', color: '#15803D' },
  APP_DIGITAL_ARREST: { name: 'Digital arrest', color: '#C2410C' },
  ATO: { name: 'Account takeover', color: '#7C3AED' },
  MULE: { name: 'Mule', color: '#1D4ED8' },
};

describe('HypothesisBar (docs/18 §3)', () => {
  it('labels every segment of at least 4% and groups the rest into "Other"', () => {
    const parts = segments(
      { GENUINE: 0.2, APP_DIGITAL_ARREST: 0.71, ATO: 0.04, MULE: 0.03, UNEXPLAINED: 0.02 },
      STYLES,
    );
    expect(parts.map((p) => p.key)).toEqual(['APP_DIGITAL_ARREST', 'GENUINE', 'ATO', 'OTHER']);
    expect(LABEL_MIN_SHARE).toBe(0.04);
    const other = parts.at(-1);
    expect(other?.share).toBeCloseTo(0.05);
    expect(other?.members?.map((m) => m.name)).toEqual(['Mule', 'UNEXPLAINED']);
  });

  it('shows labels and percentages as text, with the small ones in a tooltip', () => {
    render(
      <HypothesisBar
        label="Posterior"
        styles={STYLES}
        probs={{ GENUINE: 0.2, APP_DIGITAL_ARREST: 0.77, MULE: 0.03 }}
      />,
    );
    const bar = screen.getByRole('img', { name: /Posterior/ });
    expect(bar).toHaveAccessibleName('Posterior: Digital arrest 77%, Genuine 20%, Other 3.0%');
    expect(screen.getByText('Digital arrest 77%')).toBeInTheDocument();
    expect(screen.getByTestId('segment-OTHER')).toHaveAttribute('title', 'Mule: 3.0%');
    expect(screen.getByTestId('segment-APP_DIGITAL_ARREST')).toHaveStyle({ width: '77%' });
  });

  it('normalises, and styles a hypothesis the KB has no colour for', () => {
    const parts = segments({ GENUINE: 2, UNEXPLAINED: 2 }, STYLES);
    expect(parts.map((p) => [p.name, p.share])).toEqual([
      ['Genuine', 0.5],
      ['UNEXPLAINED', 0.5],
    ]);
  });

  it('picks the label colour with the better contrast (docs/18 §5)', () => {
    expect(textColor('#C2410C')).toBe('#FFFFFF');
    expect(textColor('#FDE68A')).toBe('#0F172A'); // a light colour gets dark text
  });

  it('stacks one bar per step in the timeline variant', () => {
    render(
      <HypothesisTimeline
        styles={STYLES}
        steps={[
          { label: 'Prior', probs: { GENUINE: 0.9, APP_DIGITAL_ARREST: 0.1 } },
          { label: '1. Call status', probs: { GENUINE: 0.4, APP_DIGITAL_ARREST: 0.6 } },
        ]}
      />,
    );
    expect(screen.getAllByRole('img').map((e) => e.getAttribute('aria-label'))).toEqual([
      'Prior: Genuine 90%, Digital arrest 10%',
      '1. Call status: Digital arrest 60%, Genuine 40%',
    ]);
  });
});
