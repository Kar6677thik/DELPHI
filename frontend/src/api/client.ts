import type { components } from './schema';

/** An RFC 9457 error body (docs/16 §1), as generated from backend/openapi.json. */
export type Problem = components['schemas']['Problem'];

export const API_BASE = '/api/v1';
/** docs/16 §1: mutating requests without this header are refused with 403 CSRF_REJECTED. */
export const CSRF_HEADER = 'X-Delphi-Csrf';
const PROBLEM_JSON = 'application/problem+json';
const SAFE_METHODS = new Set(['GET', 'HEAD']);

/** A non-2xx response. `problem` is the parsed body when the server sent problem+json. */
export class ApiError extends Error {
  readonly status: number;
  readonly problem: Problem | null;

  constructor(status: number, problem: Problem | null) {
    super(problem?.detail ?? `HTTP ${String(status)}`);
    this.name = 'ApiError';
    this.status = status;
    this.problem = problem;
  }
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
  if (!response.ok) throw new ApiError(response.status, await readProblem(response));
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
