import { http, HttpResponse } from 'msw';
import { describe, expect, it } from 'vitest';

import { ApiError, CSRF_HEADER, getJson, isUnauthenticated, postJson } from '../src/api/client';
import { server } from '../src/mocks/node';

describe('api client', () => {
  it('sends the CSRF header on mutations only (docs/16 §1)', async () => {
    const seen: Record<string, string | null> = {};
    server.use(
      http.get('/api/v1/probe', ({ request }) => {
        seen.get = request.headers.get(CSRF_HEADER);
        return HttpResponse.json({ ok: true });
      }),
      http.post('/api/v1/probe', ({ request }) => {
        seen.post = request.headers.get(CSRF_HEADER);
        return new HttpResponse(null, { status: 204 });
      }),
    );
    await expect(getJson('/probe')).resolves.toEqual({ ok: true });
    await postJson('/probe', { a: 1 });
    expect(seen).toEqual({ get: null, post: '1' });
  });

  it('parses a problem+json error body', async () => {
    server.use(
      http.get('/api/v1/probe', () =>
        HttpResponse.json(
          {
            type: 'about:blank',
            title: 'Conflict',
            status: 412,
            detail: 'This case changed.',
            code: 'VERSION_CONFLICT',
            instance: '/api/v1/probe',
            request_id: 'req-1',
          },
          { status: 412, headers: { 'Content-Type': 'application/problem+json' } },
        ),
      ),
    );
    const error: unknown = await getJson('/probe').catch((e: unknown) => e);
    expect(error).toBeInstanceOf(ApiError);
    const apiError = error as ApiError;
    expect(apiError.status).toBe(412);
    expect(apiError.problem?.code).toBe('VERSION_CONFLICT');
    expect(apiError.message).toBe('This case changed.');
    expect(isUnauthenticated(apiError)).toBe(false);
  });

  it('tolerates an error without a problem body', async () => {
    server.use(http.get('/api/v1/probe', () => new HttpResponse('bad gateway', { status: 502 })));
    const error = (await getJson('/probe').catch((e: unknown) => e)) as ApiError;
    expect(error.status).toBe(502);
    expect(error.problem).toBeNull();
    expect(error.message).toBe('HTTP 502');
  });

  it('recognises 401 as unauthenticated', () => {
    expect(isUnauthenticated(new ApiError(401, null))).toBe(true);
    expect(isUnauthenticated(new Error('x'))).toBe(false);
  });
});
