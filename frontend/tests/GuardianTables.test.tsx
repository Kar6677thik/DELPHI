import { render, screen, within } from '@testing-library/react';
import { describe, expect, it } from 'vitest';

import { CandidatesTable } from '../src/components/CandidatesTable';
import { RiskTable } from '../src/components/RiskTable';

describe('CandidatesTable (docs/18 §3)', () => {
  it('ranks eligible sources by net value, shows ₹ in the Indian grouping and the selection', () => {
    render(
      <CandidatesTable
        caption="Step 1 candidates"
        sourceNames={{ SPELL_BREAKER_POLICE: 'Spell-breaker: police check' }}
        selectedBy="EXPLORE"
        propensity={0.15}
        candidates={[
          { source_key: 'OTP_STEP_UP', eligible: false, ineligible_reason: 'COERCION_DEFEATS' },
          {
            source_key: 'SPELL_BREAKER_POLICE',
            eligible: true,
            cost_paise: 4_000,
            voi_myopic_paise: 1_23_456_00,
            net_value_paise: 1_23_416_00,
            settle_pct: 62,
            selected: true,
          },
          { source_key: 'PURPOSE_QUESTION', eligible: true, net_value_paise: 10_000_00 },
        ]}
      />,
    );
    const rows = within(screen.getByRole('table', { name: 'Step 1 candidates' })).getAllByRole(
      'row',
    );
    expect(rows.slice(1).map((r) => (r as HTMLTableRowElement).cells[0]?.textContent)).toEqual([
      'Spell-breaker: police check', // the KB's display name
      'Purpose question', // no name given: the key as words
      'OTP step up',
    ]);
    expect(screen.getByText('₹1,23,456.00')).toBeInTheDocument();
    expect(screen.getByText('No: Coercion defeats')).toBeInTheDocument();
    expect(screen.getByText('EXPLORE · p=0.15')).toBeInTheDocument();
    expect(screen.getByText('62%')).toBeInTheDocument();
  });
});

describe('RiskTable (docs/18 §3)', () => {
  it('lists expected losses lowest first and marks the lowest as recommended before a decision', () => {
    render(
      <RiskTable
        risks={{ RELEASE: 4_32_10_000, HOLD_FOR_REVIEW: 6_323_00, PROTECTIVE_HOLD: 1_761_00 }}
      />,
    );
    const rows = screen.getAllByRole('row').slice(1);
    expect(rows.map((r) => (r as HTMLTableRowElement).cells[0]?.textContent)).toEqual([
      'Protective hold(recommended)',
      'Hold for review',
      'Release',
    ]);
    expect(rows[0]).toHaveAttribute('aria-current', 'true');
    expect(screen.getByText('₹4,32,100.00')).toBeInTheDocument();
  });

  it('marks the decision taken, not the lowest-risk row, once decided (a fail-safe hold)', () => {
    render(
      <RiskTable
        risks={{ RELEASE: 1_000_00, HOLD_FOR_REVIEW: 6_323_00, PROTECTIVE_HOLD: 9_761_00 }}
        taken="HOLD_FOR_REVIEW"
      />,
    );
    const rows = screen.getAllByRole('row').slice(1);
    expect(rows.map((r) => (r as HTMLTableRowElement).cells[0]?.textContent)).toEqual([
      'Release',
      'Hold for review(decision taken)',
      'Protective hold',
    ]);
    expect(rows[1]).toHaveAttribute('aria-current', 'true');
    expect(screen.queryByText('(recommended)')).not.toBeInTheDocument();
  });
});
