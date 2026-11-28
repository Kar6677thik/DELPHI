import { useInfiniteQuery, useQuery } from '@tanstack/react-query';

import { getJson } from './client';
import type { CampaignDetail, CampaignPage } from './types';

/** Every campaign query sits under this key, so a `campaign.updated` event refreshes them all. */
export const CAMPAIGNS_ROOT_KEY = ['campaigns'] as const;

/** Campaigns per page of `GET /campaigns`. */
export const CAMPAIGN_PAGE_SIZE = 50;

/** The live campaigns (docs/16 `GET /campaigns`), highest rank first, a page at a time (docs/09 §8). */
export function useCampaigns() {
  return useInfiniteQuery({
    queryKey: [...CAMPAIGNS_ROOT_KEY, 'list'],
    queryFn: ({ pageParam }) => {
      const params = new URLSearchParams({ limit: String(CAMPAIGN_PAGE_SIZE) });
      if (pageParam) params.set('cursor', pageParam);
      return getJson<CampaignPage>(`/campaigns?${params.toString()}`);
    },
    initialPageParam: undefined as string | undefined,
    getNextPageParam: (last) => last.next_cursor ?? undefined,
  });
}

/** One campaign (docs/16 `GET /campaigns/{id}`): its oldest cases and the entities they share. */
export function useCampaign(id: string) {
  return useQuery({
    queryKey: [...CAMPAIGNS_ROOT_KEY, 'campaign', id],
    queryFn: () => getJson<CampaignDetail>(`/campaigns/${encodeURIComponent(id)}`),
  });
}
