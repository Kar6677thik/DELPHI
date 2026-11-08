import { ApiError } from '../api/client';

interface ProblemAlertProps {
  error: unknown;
  title?: string;
}

/** Renders an RFC 9457 error with its `code` and `request_id` (docs/18 §3). */
export function ProblemAlert({ error, title = 'Something went wrong' }: ProblemAlertProps) {
  const problem = error instanceof ApiError ? error.problem : null;
  // 403 FORBIDDEN: the signed-in user's role lacks the permission (docs/19 §3).
  const heading = problem?.code === 'FORBIDDEN' ? "You don't have permission to do this" : title;
  const detail = problem?.detail ?? (error instanceof Error ? error.message : 'Unexpected error.');
  return (
    <div
      role="alert"
      className="rounded-md border border-red-300 bg-red-50 p-4 text-sm text-red-900"
    >
      <p className="font-semibold">{heading}</p>
      <p className="mt-1">{detail}</p>
      {problem && (
        <p className="mt-2 font-mono text-xs">
          Code {problem.code} · Request {problem.request_id}
        </p>
      )}
    </div>
  );
}
