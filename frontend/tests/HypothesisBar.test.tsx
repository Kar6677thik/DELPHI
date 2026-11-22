import { render, screen, within } from '@testing-library/react';
import { describe, expect, it } from 'vitest';

import {
  forDark,
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

  it('shows every label and percentage as text in a legend, with the small ones grouped', () => {
    render(
      <HypothesisBar
        label="Posterior"
        styles={STYLES}
        probs={{ GENUINE: 0.2, APP_DIGITAL_ARREST: 0.77, MULE: 0.03 }}
      />,
    );
    const legend = screen.getByRole('list', { name: 'Posterior' });
    expect(
      within(legend)
        .getAllByRole('listitem')
        .map((li) => li.textContent),
    ).toEqual(['Digital arrest 77%', 'Genuine 20%', 'Other 3.0%: Mule: 3.0%']);
    expect(screen.getByTestId('legend-OTHER')).toHaveAttribute('title', 'Mule: 3.0%');
    expect(screen.getByTestId('segment-APP_DIGITAL_ARREST')).toHaveStyle({ width: '77%' });
    expect(screen.getByTestId('segment-APP_DIGITAL_ARREST')).toHaveTextContent('77%');
  });

  it('never cuts a long KB name: the legend holds the full text, outside the bar', () => {
    const long = 'Coerced payment under a fake law-enforcement call (digital arrest)';
    render(
      <HypothesisBar
        label="Step 2"
        styles={{ ...STYLES, APP_DIGITAL_ARREST: { name: long, color: '#C2410C' } }}
        probs={{ GENUINE: 0.88, APP_DIGITAL_ARREST: 0.12 }}
      />,
    );
    const item = screen.getByTestId('legend-APP_DIGITAL_ARREST');
    expect(item).toHaveTextContent(`${long} 12%`);
    expect(item).not.toHaveAttribute('aria-hidden');
    expect(item.className).not.toMatch(/truncate/);
  });

  it('keeps "Other" and an unstyled hypothesis visually distinct', () => {
    const parts = segments({ GENUINE: 0.9, UNEXPLAINED: 0.07, ATO: 0.03 }, STYLES);
    const unexplained = parts.find((p) => p.key === 'UNEXPLAINED');
    const other = parts.find((p) => p.key === 'OTHER');
    expect(unexplained?.color).not.toBe(other?.color);
    expect(textColor(unexplained?.color ?? '')).not.toBe(textColor(other?.color ?? ''));
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
    expect(textColor('#FDE68A')).toBe('#131217'); // a light colour gets dark text
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
    expect(screen.getAllByRole('list').map((e) => e.getAttribute('aria-label'))).toEqual([
      'Posterior by step',
      'Prior',
      '1. Call status',
    ]);
    expect(screen.getByRole('list', { name: '1. Call status' })).toHaveTextContent(
      'Digital arrest 60%Genuine 40%',
    );
  });
});

describe('forDark (the KB colours on the dark ground)', () => {
  it('keeps the hue and lifts a dark KB colour to at least 60% lightness', () => {
    const lifted = forDark('#1D4ED8'); // MULE's blue, set for light paper
    const [r, g, b] = [1, 3, 5].map((i) => Number.parseInt(lifted.slice(i, i + 2), 16));
    expect(b).toBeGreaterThan(r ?? 0); // still blue
    expect(b).toBeGreaterThan(g ?? 0);
    expect(
      (Math.max(r ?? 0, g ?? 0, b ?? 0) + Math.min(r ?? 0, g ?? 0, b ?? 0)) / 2 / 255,
    ).toBeGreaterThanOrEqual(0.59);
  });

  it('leaves a light colour and a non-hex value alone', () => {
    expect(forDark('#E4E4E6')).toBe('#E4E4E6');
    expect(forDark('red')).toBe('red');
  });
});
