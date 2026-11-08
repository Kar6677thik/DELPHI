import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';

import { getJson, postJson } from './client';
import type { LoginRequest, Me } from './types';

export const ME_QUERY_KEY = ['auth', 'me'] as const;
/** A 401 from sign-in means bad credentials, not an ended session (see createQueryClient). */
export const LOGIN_MUTATION_KEY = ['auth', 'login'] as const;

export function useMe() {
  return useQuery({ queryKey: ME_QUERY_KEY, queryFn: () => getJson<Me>('/auth/me') });
}

export function useLogin() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationKey: LOGIN_MUTATION_KEY,
    mutationFn: (request: LoginRequest) => postJson('/auth/login', request),
    // Reset, not invalidate: after an expired session the query sits in `error`, and an
    // invalidated query keeps that status while it refetches, so RequireSession would send the
    // user straight back to /login. A reset starts the new session from `pending`.
    onSuccess: () => queryClient.resetQueries({ queryKey: ME_QUERY_KEY }),
  });
}

export function useLogout() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: () => postJson('/auth/logout'),
    // Signed out or not, nothing cached for this user may outlive the session.
    onSettled: () => {
      queryClient.clear();
    },
  });
}
