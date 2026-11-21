import { render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { EvidenceCard, factText, posteriorShift } from '../src/components/EvidenceCard';
import {
  NarrativeWithCitations,
  factsId,
  narrativeLabel,
  parseNarrative,
} from '../src/components/NarrativeWithCitations';
import { mockEvidence } from './mocks/handlers';

const STYLES = {
  APP_DIGITAL_ARREST: { name: 'Coerced victim — digital arrest', color: '#B91C1C' },
  MULE: { name: 'Mule account', color: '#7C3AED' },
};
const ID = '01930000-0000-7000-8000-0000000000e1';
const label = (id: string) => (id === ID ? 'Inbound fan in' : undefined);

afterEach(() => {
  vi.useRealTimers();
});

describe('parseNarrative (docs/15 §4)', () => {
  it('splits the text around each [record_id] citation', () => {
    expect(parseNarrative(`Fourteen payers [${ID}]. Dwell was short [${ID}]`)).toEqual([
      { text: 'Fourteen payers' },
      { citation: ID },
      { text: '. Dwell was short' },
      { citation: ID },
    ]);
  });

  it('leaves text without citations, and other brackets, as text', () => {
    expect(parseNarrative('No citation [here].')).toEqual([{ text: 'No citation [here].' }]);
  });
});

describe('NarrativeWithCitations', () => {
  it('labels who wrote it (docs/09 §2 item 5)', () => {
    expect(narrativeLabel('LLM')).toBe('AI-written summary');
    expect(narrativeLabel('TEMPLATE')).toBe('Template summary');
    expect(narrativeLabel(null)).toBe('Template summary');
  });

  it('scrolls to the cited fact row and highlights it', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime.bind(vi) });
    const scroll = vi.fn();
    render(
      <>
        <NarrativeWithCitations
          narrative={`Fourteen payers [${ID}].`}
          source="LLM"
          citationLabel={label}
        />
        <table id={factsId(ID)} tabIndex={-1}>
          <tbody>
            <tr>
              <td>14</td>
            </tr>
          </tbody>
        </table>
      </>,
    );
    const facts = document.getElementById(factsId(ID)) as HTMLElement;
    facts.scrollIntoView = scroll;
    expect(screen.getByText('AI-written summary')).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: 'Show the facts: Inbound fan in' }));
    expect(scroll).toHaveBeenCalledOnce();
    expect(facts).toHaveFocus();
    expect(facts).toHaveClass('ring-2');
    vi.advanceTimersByTime(2_000);
    expect(facts).not.toHaveClass('ring-2');
  });

  it('renders a citation to a record not on the page as text, not a dead button', () => {
    render(
      <NarrativeWithCitations
        narrative={`Gone [01930000-0000-7000-8000-0000000000ff].`}
        source="TEMPLATE"
        citationLabel={label}
      />,
    );
    expect(screen.queryByRole('button')).not.toBeInTheDocument();
    expect(screen.getByText('(cited record not shown)')).toBeInTheDocument();
  });
});

describe('EvidenceCard (docs/09 §2 item 5)', () => {
  it('shows the outcome, the posterior shift, the facts and the narrative', () => {
    render(<EvidenceCard record={mockEvidence()} styles={STYLES} citationLabel={label} />);
    const card = within(screen.getByRole('article', { name: /Inbound fan in/ }));
    expect(card.getByText('High fan in')).toBeInTheDocument();
    const shift = card.getByRole('list', { name: 'Posterior shift' });
    expect(shift).toHaveTextContent('Coerced victim — digital arrest: 50% → 70%');
    expect(shift).toHaveTextContent('Mule account: 20% → 25%');
    const facts = card.getByRole('table', { name: 'Facts of Inbound fan in' });
    expect(within(facts).getByRole('row', { name: 'Unrelated payers 14' })).toBeInTheDocument();
    expect(within(facts).getByRole('row', { name: 'Window h 48' })).toBeInTheDocument();
    expect(card.getByText('Template summary')).toBeInTheDocument();
    expect(
      card.getByRole('button', { name: 'Show the facts: Inbound fan in' }),
    ).toBeInTheDocument();
  });

  it('says the summary is being written until it arrives', () => {
    render(
      <EvidenceCard
        record={mockEvidence({ narrative: null })}
        styles={STYLES}
        citationLabel={label}
      />,
    );
    expect(screen.getByText('The summary is being written…')).toBeInTheDocument();
  });

  it('keeps the top two hypotheses after the record', () => {
    expect(
      posteriorShift({
        posterior_before: { A: 0.6, B: 0.3, C: 0.1 },
        posterior_after: { A: 0.2, B: 0.3, C: 0.5 },
      }),
    ).toEqual([
      { key: 'C', before: 0.1, after: 0.5 },
      { key: 'B', before: 0.3, after: 0.3 },
    ]);
  });

  it('formats facts: paise as ₹, shares as %, booleans as words, nulls as a dash', () => {
    expect(factText('departure_share', 0.83)).toBe('83%');
    expect(factText('max_prior_amount_paise', 480_000)).toBe('₹4,800.00');
    expect(factText('new_city', true)).toBe('Yes');
    expect(factText('speed_kmh', null)).toBe('—');
    expect(factText('last_city', 'Pune')).toBe('Pune');
    expect(factText('extra', { a: 1 })).toBe('{"a":1}');
  });
});
