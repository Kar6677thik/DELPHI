import { screen, within } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import { describe, expect, it } from 'vitest';

import { parseDdPack } from '../src/api/cases';
import { problem, setMockSession } from './mocks/handlers';
import { server } from './mocks/node';
import { renderApp } from './renderApp';

const CASE_ID = '01930000-0000-7000-8000-000000000001';
const URL = `/api/v1/cases/${CASE_ID}/dd-pack`;
const TITLES = [
  'Header',
  'Prior',
  'Observation timeline',
  'Guardrails applied',
  'Decisions',
  'Narratives',
  'Liens and tickets',
  'Audit verification',
  'Method note',
];

/** A pack as `GET /cases/{id}/dd-pack` returns it (docs/09 §7, #228). */
function pack(complete: boolean) {
  return {
    case_id: CASE_ID,
    complete,
    sections: TITLES.map((title, i) => ({
      number: i + 1,
      title,
      required: i !== 6,
      content:
        i === 0
          ? { case_id: CASE_ID, protocol: 'COERCION', amount_paise: 48_000_000, vulnerable: true }
          : i === 2
            ? [{ origin: 'GUARDIAN', source: 'CALL_STATUS', outcome: 'ON_CALL_UNKNOWN_NUMBER' }]
            : i === 4 && !complete
              ? null
              : i === 6
                ? 'Not applicable'
                : i === 8
                  ? 'Bayesian decision engine; likelihoods from KB version 1.'
                  : { ok: true },
      ...(i === 4 && !complete ? { note: 'The case has no disposition yet.' } : {}),
      ...(i === 6 ? { note: "No chase for this case's subject (the chase arrives in M6)." } : {}),
    })),
  };
}

/** The pack's status line, whose parts are styled apart: match the whole paragraph's text. */
function packStatus(text: string) {
  return (_: string, el: Element | null) =>
    el?.getAttribute('role') === 'status' && el.textContent.replace(/\s+/g, ' ').trim() === text;
}

describe('the case DD pack page (docs/18 §2, docs/09 §7)', () => {
  it('shows all nine sections in order with the completeness', async () => {
    setMockSession('viewer');
    server.use(http.get(URL, () => HttpResponse.json(pack(true))));
    renderApp(`/cases/${CASE_ID}/dd-pack`);
    expect(await screen.findByRole('heading', { name: 'Due-diligence pack' })).toBeInTheDocument();
    expect(await screen.findByText(packStatus(`Case ${CASE_ID} · Complete`))).toBeVisible();
    const headings = screen.getAllByRole('heading', { level: 2 }).map((h) => h.textContent);
    expect(headings).toHaveLength(TITLES.length);
    TITLES.forEach((title, i) => {
      expect(headings[i]).toContain(`${String(i + 1)}. ${title}`);
    });
    const header = within(screen.getByRole('region', { name: /1\. Header/ }));
    expect(header.getByRole('row', { name: 'Protocol COERCION' })).toBeInTheDocument();
    expect(header.getByRole('row', { name: 'Vulnerable Yes' })).toBeInTheDocument();
    const liens = within(screen.getByRole('region', { name: /7\. Liens and tickets/ }));
    expect(liens.getByText('Not applicable')).toBeInTheDocument();
    expect(liens.getByText(/No chase for this case/)).toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'Open the printable page' })).toHaveAttribute(
      'href',
      `${URL}?format=html`,
    );
  });

  it('marks an incomplete pack and the required section that is missing', async () => {
    setMockSession('viewer');
    server.use(http.get(URL, () => HttpResponse.json(pack(false))));
    renderApp(`/cases/${CASE_ID}/dd-pack`);
    expect(await screen.findByText(packStatus(`Case ${CASE_ID} · Incomplete`))).toBeVisible();
    const decisions = within(screen.getByRole('region', { name: /5\. Decisions/ }));
    expect(decisions.getByRole('heading')).toHaveTextContent('Required · missing');
    expect(decisions.getByText('The case has no disposition yet.')).toBeInTheDocument();
  });

  it('shows the problem when the pack cannot load', async () => {
    setMockSession('viewer');
    server.use(
      http.get(URL, ({ request }) =>
        problem(404, 'Not Found', 'NOT_FOUND', 'No case with this id.', request.url),
      ),
    );
    renderApp(`/cases/${CASE_ID}/dd-pack`);
    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent('Could not load the DD pack');
    expect(alert).toHaveTextContent('NOT_FOUND');
  });
});

describe('parseDdPack', () => {
  it('reads the fields with checks and skips a malformed section', () => {
    expect(
      parseDdPack({
        case_id: CASE_ID,
        complete: true,
        sections: [
          { number: 1, title: 'Header', required: true, content: { a: 1 } },
          { number: 'two', title: 'Prior' },
          null,
          { number: 9, title: 'Method note', content: 'text', note: 7 },
        ],
      }),
    ).toEqual({
      caseId: CASE_ID,
      complete: true,
      sections: [
        { number: 1, title: 'Header', required: true, content: { a: 1 }, note: null },
        { number: 9, title: 'Method note', required: false, content: 'text', note: null },
      ],
    });
  });

  it('reads nothing from a body that is not a pack', () => {
    expect(parseDdPack('<html>')).toEqual({ caseId: '', complete: false, sections: [] });
  });
});
