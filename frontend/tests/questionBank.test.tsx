import { screen, within } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import { beforeEach, describe, expect, it } from 'vitest';

import type { QuestionVariantView } from '../src/api/types';
import { problem, setMockSession } from './mocks/handlers';
import { server } from './mocks/node';
import { renderApp } from './renderApp';

/** The question bank review (`/kb/questions`, docs/18 §2, #110). */

const ORIGINAL = {
  variant_id: 'PQ-en-1',
  text: 'What is this payment for?',
  options: [
    { key: 'FAMILY_OR_FRIEND', text: 'Family or a friend' },
    { key: 'OTHER', text: 'Something else' },
  ],
};

function variant(overrides: Partial<QuestionVariantView> = {}): QuestionVariantView {
  return {
    id: '01930000-0000-7000-8000-0000000000b1',
    probe_key: 'PURPOSE_QUESTION',
    language: 'te',
    text: 'ఈ చెల్లింపు దేనికోసం?',
    options: [
      { key: 'FAMILY_OR_FRIEND', text: 'కుటుంబం లేదా స్నేహితుడు' },
      { key: 'OTHER', text: 'ఇంకేదైనా' },
    ],
    status: 'PENDING_REVIEW',
    generated_by: 'LLM',
    llm_call_id: '01930000-0000-7000-8000-0000000000c1',
    reviewed_by: null,
    reviewed_at: null,
    included_in_kb_version: null,
    created_at: '2026-11-26T10:00:00Z',
    original: ORIGINAL,
    ...overrides,
  };
}

const TE = variant();
const HI = variant({
  id: '01930000-0000-7000-8000-0000000000b2',
  language: 'hi',
  text: 'यह भुगतान किसलिए है?',
});

interface Seen {
  reviews: { id: string; body: unknown }[];
  generated: unknown[];
  batches: number;
}

function serve(pending: QuestionVariantView[], approved: QuestionVariantView[] = []): Seen {
  const seen: Seen = { reviews: [], generated: [], batches: 0 };
  server.use(
    http.get('/api/v1/question-bank/variants', ({ request }) => {
      const status = new URL(request.url).searchParams.get('status');
      const items = status === 'APPROVED' ? approved : pending;
      return HttpResponse.json({ items, next_cursor: null });
    }),
    http.post('/api/v1/question-bank/variants/:id/review', async ({ params, request }) => {
      const body = await request.json();
      seen.reviews.push({ id: String(params.id), body });
      return HttpResponse.json({ ...TE, status: 'APPROVED' });
    }),
    http.post('/api/v1/question-bank/generate', async ({ request }) => {
      seen.generated.push(await request.json());
      return HttpResponse.json({ job_id: crypto.randomUUID(), created: true }, { status: 202 });
    }),
    http.post('/api/v1/question-bank/approve-batch', () => {
      seen.batches += 1;
      return HttpResponse.json({ kb_version_id: 7, variant_ids: [TE.id] }, { status: 201 });
    }),
  );
  return seen;
}

describe('the question bank review', () => {
  beforeEach(() => {
    setMockSession('fde');
  });

  it('shows each draft beside its English original', async () => {
    serve([TE, HI]);
    renderApp('/kb/questions');
    const card = await screen.findByRole('article', { name: /Purpose question · Telugu/ });
    expect(within(card).getByText(ORIGINAL.text)).toBeVisible();
    expect(within(card).getByText(TE.text)).toHaveAttribute('lang', 'te');
    expect(within(card).getByText('AI-drafted')).toBeVisible();
    expect(screen.getByRole('article', { name: /Purpose question · Hindi/ })).toBeVisible();
  });

  it('approves, approves with an edit, and rejects', async () => {
    const seen = serve([TE, HI]);
    const { user } = renderApp('/kb/questions');
    const te = await screen.findByRole('article', { name: /Telugu/ });
    await user.click(within(te).getByRole('button', { name: 'Edit' }));
    const box = within(te).getByLabelText('Edited question');
    await user.clear(box);
    await user.type(box, 'ఈ డబ్బు ఎందుకు?');
    await user.click(within(te).getByRole('button', { name: 'Approve with edit' }));
    const hi = screen.getByRole('article', { name: /Hindi/ });
    await user.click(within(hi).getByRole('button', { name: 'Reject' }));
    expect(seen.reviews).toEqual([
      { id: TE.id, body: { decision: 'APPROVE', edited_text: 'ఈ డబ్బు ఎందుకు?' } },
      { id: HI.id, body: { decision: 'REJECT', edited_text: null } },
    ]);
  });

  it('shows why a review was refused', async () => {
    serve([TE]);
    server.use(
      http.post('/api/v1/question-bank/variants/:id/review', ({ request }) =>
        problem(
          409,
          'Conflict',
          'INVALID_STATE_TRANSITION',
          'The variant is already APPROVED.',
          request.url,
        ),
      ),
    );
    const { user } = renderApp('/kb/questions');
    const te = await screen.findByRole('article', { name: /Telugu/ });
    await user.click(within(te).getByRole('button', { name: 'Approve' }));
    expect(await within(te).findByRole('alert')).toHaveTextContent(
      'The variant is already APPROVED.',
    );
  });

  it('asks for drafts in the languages chosen', async () => {
    const seen = serve([]);
    const { user } = renderApp('/kb/questions');
    expect(await screen.findByText('No draft is waiting for review.')).toBeVisible();
    await user.selectOptions(screen.getByLabelText('Question'), 'COLLECT_INTENT_CHECK');
    await user.click(screen.getByRole('checkbox', { name: 'Hindi' }));
    await user.click(screen.getByRole('button', { name: 'Draft' }));
    expect(await screen.findByText(/Drafting started/)).toBeVisible();
    expect(seen.generated).toEqual([
      { probe_key: 'COLLECT_INTENT_CHECK', languages: ['te', 'hi'] },
    ]);
  });

  it('bundles the approved variants into a candidate KB version', async () => {
    const seen = serve([], [variant({ status: 'APPROVED' })]);
    const { user } = renderApp('/kb/questions');
    expect(await screen.findByText(/1 approved variant is waiting/)).toBeVisible();
    await user.click(screen.getByRole('button', { name: 'Approve batch' }));
    expect(await screen.findByText(/Candidate KB version 7 holds 1 variant/)).toBeVisible();
    expect(seen.batches).toBe(1);
  });

  it('lets a viewer read the queue but not act on it', async () => {
    setMockSession('viewer');
    serve([TE]);
    renderApp('/kb/questions');
    const te = await screen.findByRole('article', { name: /Telugu/ });
    expect(within(te).queryByRole('button')).toBeNull();
    expect(screen.queryByRole('button', { name: 'Draft' })).toBeNull();
    expect(screen.queryByRole('button', { name: 'Approve batch' })).toBeNull();
  });
});
