import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';

import { ApiError } from '../src/api/client';
import { ProblemAlert } from '../src/components/ProblemAlert';

describe('ProblemAlert', () => {
  it('shows the detail, code and request id of a problem', () => {
    const error = new ApiError(429, {
      type: 'about:blank',
      title: 'Too Many Requests',
      status: 429,
      detail: 'Too many sign-in attempts.',
      code: 'RATE_LIMITED',
      instance: '/api/v1/auth/login',
      request_id: '0192f0aa-req',
    });
    render(<ProblemAlert error={error} title="Sign-in failed" />);
    const alert = screen.getByRole('alert');
    expect(alert).toHaveTextContent('Sign-in failed');
    expect(alert).toHaveTextContent('Too many sign-in attempts.');
    expect(alert).toHaveTextContent('Code RATE_LIMITED · Request 0192f0aa-req');
  });

  it('says plainly when the role lacks the permission (403 FORBIDDEN)', () => {
    const error = new ApiError(403, {
      type: 'about:blank',
      title: 'Forbidden',
      status: 403,
      detail: 'You do not have permission to do this.',
      code: 'FORBIDDEN',
      instance: '/api/v1/jobs',
      request_id: 'req-403',
    });
    render(<ProblemAlert error={error} title="Could not load the jobs" />);
    const alert = screen.getByRole('alert');
    expect(alert).toHaveTextContent("You don't have permission to do this");
    expect(alert).not.toHaveTextContent('Could not load the jobs');
    expect(alert).toHaveTextContent('Code FORBIDDEN');
  });

  it('falls back to the message of a plain error', () => {
    render(<ProblemAlert error={new TypeError('Failed to fetch')} />);
    expect(screen.getByRole('alert')).toHaveTextContent('Something went wrongFailed to fetch');
    expect(screen.queryByText(/Request/)).not.toBeInTheDocument();
  });

  it('handles a thrown non-error', () => {
    render(<ProblemAlert error="boom" />);
    expect(screen.getByRole('alert')).toHaveTextContent('Unexpected error.');
  });
});
