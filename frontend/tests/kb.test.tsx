import { screen, within } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import { describe, expect, it } from 'vitest';

import { afterAnswer, parseKb, separation } from '../src/api/kb';
import { keyText } from '../src/lib/format';
import { setMockSession } from './mocks/handlers';
import { server } from './mocks/node';
import { renderApp } from './renderApp';

/** The knowledge base page (`/kb`, docs/18 §2, part of #119), on a small two-version KB. */

const CONTENT = {
  name: 'Test KB',
  description: 'A small KB for the page.',
  hypothesis_order: ['GENUINE', 'APP_DIGITAL_ARREST'],
  source_order: ['CALL_STATUS', 'SPELL_BREAKER_DIGITAL_ARREST', 'ACCOUNT_PROFILE'],
  hypotheses: {
    GENUINE: {
      display_name: 'Genuine',
      color: '#15803D',
      kind: 'GENUINE',
      description: 'Acting freely.',
      likelihoods: {
        CALL_STATUS: { probs: [0.9, 0.1], ess: 20 },
        SPELL_BREAKER_DIGITAL_ARREST: { probs: [0.95, 0.05], ess: 20 },
        ACCOUNT_PROFILE: { probs: [0.5, 0.5], ess: 20 },
      },
    },
    APP_DIGITAL_ARREST: {
      display_name: 'Coerced victim — digital arrest',
      color: '#C2410C',
      kind: 'TYPOLOGY',
      description: 'Coached live by a fraudster.',
      likelihoods: {
        CALL_STATUS: { probs: [0.2, 0.8], ess: 20 },
        SPELL_BREAKER_DIGITAL_ARREST: { probs: [0.25, 0.75], ess: 20 },
        ACCOUNT_PROFILE: { probs: [0.5, 0.5], ess: 20 },
      },
    },
  },
  sources: {
    CALL_STATUS: {
      kind: 'PROBE',
      mode: 'PASSIVE',
      friction_tier: 'NONE',
      outcomes: ['NO_CALL', 'ON_CALL_UNKNOWN_NUMBER'],
      description: 'Whether a call is live.',
      question: false,
    },
    SPELL_BREAKER_DIGITAL_ARREST: {
      kind: 'PROBE',
      mode: 'ACTIVE',
      friction_tier: 'MEDIUM',
      outcomes: ['NO', 'YES'],
      description: 'The spell-breaker question.',
      question: true,
    },
    ACCOUNT_PROFILE: {
      kind: 'EVIDENCE',
      mode: null,
      friction_tier: 'NONE',
      outcomes: ['NEW', 'ESTABLISHED'],
      description: 'Account age.',
      question: false,
    },
  },
  question_bank: {
    SPELL_BREAKER_DIGITAL_ARREST: {
      variants: [
        {
          language: 'en',
          status: 'APPROVED',
          text: 'Has anyone told you to keep this payment secret?',
          options: [
            { key: 'NO', text: 'No' },
            { key: 'YES', text: 'Yes, someone did' },
          ],
        },
      ],
    },
  },
};

const version = (id: number, status: string, parent: number | null) => ({
  id,
  status,
  parent_id: parent,
  change_summary: id === 1 ? 'Seed knowledge base.' : 'Spell-breaker rows re-elicited.',
  created_by: null,
  created_at: '2026-11-08T00:00:00Z',
  activated_at: null,
  content_sha256: 'abc123def4567890',
});

function serveKb() {
  server.use(
    http.get('/api/v1/kb/versions', () =>
      HttpResponse.json({
        items: [version(2, 'SHADOW', 1), version(1, 'ACTIVE', null)],
        next_cursor: null,
      }),
    ),
    http.get('/api/v1/kb/versions/:id/diff', () =>
      HttpResponse.json({
        version_id: 2,
        against_id: 1,
        changes: [
          {
            path: 'hypotheses.GENUINE.likelihoods.CALL_STATUS.ess',
            op: 'changed',
            before: 20,
            after: 35,
          },
        ],
      }),
    ),
    http.get('/api/v1/kb/versions/:id', ({ params }) => {
      const id = Number(params.id);
      return HttpResponse.json({
        ...version(id, id === 1 ? 'ACTIVE' : 'SHADOW', id === 1 ? null : 1),
        approved_by: null,
        retired_at: null,
        rollback_reason: null,
        content: CONTENT,
      });
    }),
  );
}

describe('the KB maths', () => {
  const kb = parseKb(CONTENT);

  it('reads the content in the KB order, with the question and its answers', () => {
    expect(kb.hypotheses.map((h) => h.key)).toEqual(['GENUINE', 'APP_DIGITAL_ARREST']);
    const spell = kb.sources.find((s) => s.key === 'SPELL_BREAKER_DIGITAL_ARREST');
    expect(spell?.question).toBe('Has anyone told you to keep this payment secret?');
    expect(spell?.answers.YES).toBe('Yes, someone did');
  });

  it('gives no separation to a signal every explanation predicts alike', () => {
    const profile = kb.sources.find((s) => s.key === 'ACCOUNT_PROFILE');
    const call = kb.sources.find((s) => s.key === 'CALL_STATUS');
    if (!profile || !call) throw new Error('missing source');
    expect(separation(profile, kb.hypotheses)).toBeCloseTo(0, 10);
    expect(separation(call, kb.hypotheses)).toBeGreaterThan(0.3);
  });

  it('updates an even belief by Bayes’ rule', () => {
    const spell = kb.sources.find((s) => s.key === 'SPELL_BREAKER_DIGITAL_ARREST');
    if (!spell) throw new Error('missing source');
    const after = afterAnswer(spell, 1, kb.hypotheses);
    expect(after.APP_DIGITAL_ARREST).toBeCloseTo(0.75 / 0.8, 10);
    expect(after.GENUINE).toBeCloseTo(0.05 / 0.8, 10);
  });

  it('keeps acronyms when naming keys', () => {
    expect(keyText('OTP_STEP_UP')).toBe('OTP step up');
    expect(keyText('CRM_CONTACT_HISTORY')).toBe('CRM contact history');
  });
});

describe('/kb', () => {
  it('opens on the active version and shows what each answer would do', async () => {
    setMockSession('viewer');
    serveKb();
    const { user } = renderApp('/kb');
    expect(await screen.findByRole('heading', { name: 'What DELPHI can believe' })).toBeVisible();
    const versions = screen.getByRole('list', { name: 'KB versions, newest first' });
    expect(within(versions).getByRole('button', { current: true })).toHaveTextContent('v1');
    expect(screen.getByText('Coached live by a fraudster.')).toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'Export as YAML' })).toHaveAttribute(
      'href',
      '/api/v1/kb/versions/1/export',
    );
    // The seed has no parent, so there is nothing to diff.
    expect(screen.queryByRole('heading', { name: /What changed/ })).not.toBeInTheDocument();

    const signal = screen.getByRole('region', { name: 'Spell breaker digital arrest' });
    expect(within(signal).getByText(/keep this payment secret/)).toBeInTheDocument();
    await user.click(within(signal).getByRole('button', { name: 'Yes, someone did' }));
    const belief = within(signal).getByRole('list', { name: 'Belief after this answer' });
    expect(within(belief).getByText(/94%/)).toBeInTheDocument();
    expect(within(signal).getByText(/points to coerced victim — digital arrest/)).toBeVisible();

    await user.click(screen.getByRole('button', { name: /^Call status/ }));
    expect(screen.getByRole('region', { name: 'Call status' })).toBeInTheDocument();
  });

  it('shows a version’s changes from its parent', async () => {
    setMockSession('viewer');
    serveKb();
    const { user } = renderApp('/kb');
    const versions = await screen.findByRole('list', { name: 'KB versions, newest first' });
    await user.click(within(versions).getByRole('button', { name: /v2/ }));
    expect(await screen.findByRole('heading', { name: 'What changed from v1' })).toBeVisible();
    const changes = await screen.findByRole('list', { name: 'Changes' });
    expect(
      within(changes).getByText('hypotheses.GENUINE.likelihoods.CALL_STATUS.ess'),
    ).toBeVisible();
  });
});
