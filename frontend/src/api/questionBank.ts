import { useInfiniteQuery, useMutation, useQueryClient } from '@tanstack/react-query';

import { apiFetch, getJson } from './client';
import type {
  BatchView,
  GenerateQueued,
  GenerateRequest,
  QuestionVariantPage,
  QuestionVariantView,
  ReviewRequest,
  VariantStatus,
} from './types';

/** The question bank review's queries share this key, so every action refreshes them. */
export const QUESTION_BANK_KEY = ['question-bank'] as const;

/** One page of a list: oldest first, as the reviewers work through it. */
export const VARIANT_PAGE_SIZE = 100;

/**
 * `GET /question-bank/variants` (docs/16 "KB"), each one beside the English original, a page at
 * a time. `included: false` with `APPROVED` is what "Approve batch" would take.
 */
export function useVariants(status: VariantStatus, included?: boolean) {
  return useInfiniteQuery({
    queryKey: [...QUESTION_BANK_KEY, 'variants', status, included ?? null],
    queryFn: ({ pageParam }) => {
      const params = new URLSearchParams({ status, limit: String(VARIANT_PAGE_SIZE) });
      if (included !== undefined) params.set('included', String(included));
      if (pageParam) params.set('cursor', pageParam);
      return getJson<QuestionVariantPage>(`/question-bank/variants?${params.toString()}`);
    },
    initialPageParam: undefined as string | undefined,
    getNextPageParam: (last) => last.next_cursor ?? undefined,
  });
}

async function post<T>(path: string, body: unknown, idempotent: boolean): Promise<T> {
  const headers: Record<string, string> = { 'Content-Type': 'application/json' };
  // docs/16 §1: a retried request with the same key does the work once.
  if (idempotent) headers['Idempotency-Key'] = crypto.randomUUID();
  const response = await apiFetch(path, { method: 'POST', headers, body: JSON.stringify(body) });
  return (await response.json()) as T;
}

/** Refreshes the lists after the action. ``refreshOnError: false`` leaves them as they were, so
 * a refused review's message stays on its card until the reviewer refreshes. */
function useBankMutation<I, O>(run: (input: I) => Promise<O>, refreshOnError = true) {
  const client = useQueryClient();
  const refresh = () => {
    void client.invalidateQueries({ queryKey: QUESTION_BANK_KEY });
  };
  return useMutation({
    mutationFn: run,
    onSuccess: refresh,
    onError: refreshOnError ? refresh : undefined,
  });
}

/** Refreshes every question bank list, e.g. after a refused review. */
export function useRefreshQuestionBank() {
  const client = useQueryClient();
  return () => {
    void client.invalidateQueries({ queryKey: QUESTION_BANK_KEY });
  };
}

/** `POST /question-bank/generate`: an `LLM_QUESTION_VARIANTS` job; drafts arrive as it ends. */
export function useGenerateVariants() {
  return useBankMutation((request: GenerateRequest) =>
    post<GenerateQueued>('/question-bank/generate', request, true),
  );
}

/** `POST /question-bank/variants/{id}/review`: 409 once someone else reviewed it. */
export function useReviewVariant() {
  return useBankMutation(
    ({ id, ...body }: ReviewRequest & { id: string }) =>
      post<QuestionVariantView>(`/question-bank/variants/${id}/review`, body, false),
    false,
  );
}

/** `POST /question-bank/approve-batch`: a CANDIDATE KB version with the approved variants. */
export function useApproveBatch() {
  return useBankMutation(() => post<BatchView>('/question-bank/approve-batch', {}, true));
}
