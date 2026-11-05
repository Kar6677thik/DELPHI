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
