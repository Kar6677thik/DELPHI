import type { components } from './schema';

/** An RFC 9457 error body (docs/16 §1), as generated from backend/openapi.json. */
export type Problem = components['schemas']['Problem'];

export const API_BASE = '/api/v1';
/** docs/16 §1: mutating requests without this header are refused with 403 CSRF_REJECTED. */
export const CSRF_HEADER = 'X-Delphi-Csrf';
const PROBLEM_JSON = 'application/problem+json';
const SAFE_METHODS = new Set(['GET', 'HEAD']);

/**
 * A non-2xx response. `problem` is the parsed body when the server sent problem+json;
 * `retryAfterSeconds` is the `Retry-After` header (429 RATE_LIMITED, docs/16 §1), if any.
 */
export class ApiError extends Error {
  readonly status: number;
  readonly problem: Problem | null;
  readonly retryAfterSeconds: number | null;

  constructor(status: number, problem: Problem | null, retryAfterSeconds: number | null = null) {
    super(problem?.detail ?? `HTTP ${String(status)}`);
    this.name = 'ApiError';
    this.status = status;
    this.problem = problem;
    this.retryAfterSeconds = retryAfterSeconds;
  }
}

/** `Retry-After` is either a number of seconds or an HTTP date (RFC 9110 §10.2.3). */
export function parseRetryAfter(value: string | null, now: number = Date.now()): number | null {
  if (value === null || value.trim() === '') return null;
  const trimmed = value.trim();
  if (/^\d+$/.test(trimmed)) return Number(trimmed);
  const at = Date.parse(trimmed);
  return Number.isNaN(at) ? null : Math.max(0, Math.ceil((at - now) / 1000));
}

async function readProblem(response: Response): Promise<Problem | null> {
  if (!response.headers.get('content-type')?.startsWith(PROBLEM_JSON)) return null;
  try {
    return (await response.json()) as Problem;
  } catch {
    return null;
  }
}

/** Same-origin fetch under /api/v1 that adds the CSRF header and throws ApiError on failure. */
export async function apiFetch(path: string, init: RequestInit = {}): Promise<Response> {
  const method = (init.method ?? 'GET').toUpperCase();
  const headers = new Headers(init.headers);
  headers.set('Accept', `application/json, ${PROBLEM_JSON}`);
  if (!SAFE_METHODS.has(method)) headers.set(CSRF_HEADER, '1');
  const url = new URL(`${API_BASE}${path}`, window.location.origin);
  const response = await fetch(url, { ...init, method, headers, credentials: 'same-origin' });
  if (!response.ok) {
    const retryAfter = parseRetryAfter(response.headers.get('retry-after'));
    throw new ApiError(response.status, await readProblem(response), retryAfter);
  }
  return response;
}

export async function getJson<T>(path: string): Promise<T> {
  const response = await apiFetch(path);
  return (await response.json()) as T;
}

export async function postJson(path: string, body?: unknown): Promise<void> {
  await apiFetch(
    path,
    body === undefined
      ? { method: 'POST' }
      : {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(body),
        },
  );
}

export function isUnauthenticated(error: unknown): boolean {
  return error instanceof ApiError && error.status === 401;
}

export function isRateLimited(error: unknown): error is ApiError {
  return error instanceof ApiError && error.status === 429;
}
