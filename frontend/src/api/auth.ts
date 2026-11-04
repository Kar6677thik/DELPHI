import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';

import { getJson, postJson } from './client';
import type { LoginRequest, Me } from './pending-contracts';

export const ME_QUERY_KEY = ['auth', 'me'] as const;

export function useMe() {
  return useQuery({ queryKey: ME_QUERY_KEY, queryFn: () => getJson<Me>('/auth/me') });
}

export function useLogin() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (request: LoginRequest) => postJson('/auth/login', request),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ME_QUERY_KEY }),
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
