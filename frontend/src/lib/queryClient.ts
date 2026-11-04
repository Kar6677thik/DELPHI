import { QueryClient } from '@tanstack/react-query';

import { ApiError } from '../api/client';
import { QUERY_MAX_RETRIES } from './config';

const FIRST_SERVER_ERROR = 500;

function shouldRetry(failureCount: number, error: unknown): boolean {
  if (error instanceof ApiError && error.status < FIRST_SERVER_ERROR) return false;
  return failureCount < QUERY_MAX_RETRIES;
}

export function createQueryClient(): QueryClient {
  return new QueryClient({ defaultOptions: { queries: { retry: shouldRetry } } });
}
