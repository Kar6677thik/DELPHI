import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';

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

/** One page of the review queue: oldest first, as the reviewers work through it. */
export const VARIANT_PAGE_SIZE = 100;

/** `GET /question-bank/variants` (docs/16 "KB"): each one beside the English original. */
export function useVariants(status: VariantStatus) {
  const params = new URLSearchParams({ status, limit: String(VARIANT_PAGE_SIZE) });
  return useQuery({
    queryKey: [...QUESTION_BANK_KEY, 'variants', status],
    queryFn: () => getJson<QuestionVariantPage>(`/question-bank/variants?${params.toString()}`),
  });
}

async function post<T>(path: string, body: unknown, idempotent: boolean): Promise<T> {
  const headers: Record<string, string> = { 'Content-Type': 'application/json' };
  // docs/16 §1: a retried request with the same key does the work once.
  if (idempotent) headers['Idempotency-Key'] = crypto.randomUUID();
  const response = await apiFetch(path, { method: 'POST', headers, body: JSON.stringify(body) });
  return (await response.json()) as T;
}

function useBankMutation<I, O>(run: (input: I) => Promise<O>) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: run,
    onSettled: () => {
      void client.invalidateQueries({ queryKey: QUESTION_BANK_KEY });
    },
  });
}

/** `POST /question-bank/generate`: an `LLM_QUESTION_VARIANTS` job; drafts arrive as it ends. */
export function useGenerateVariants() {
  return useBankMutation((request: GenerateRequest) =>
    post<GenerateQueued>('/question-bank/generate', request, true),
  );
}

/** `POST /question-bank/variants/{id}/review`: 409 once someone else reviewed it. */
export function useReviewVariant() {
  return useBankMutation(({ id, ...body }: ReviewRequest & { id: string }) =>
    post<QuestionVariantView>(`/question-bank/variants/${id}/review`, body, false),
  );
}

/** `POST /question-bank/approve-batch`: a CANDIDATE KB version with the approved variants. */
export function useApproveBatch() {
  return useBankMutation(() => post<BatchView>('/question-bank/approve-batch', {}, true));
}
