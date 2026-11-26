import { useQuery } from '@tanstack/react-query';

import { getJson } from './client';
import type { CampaignDetail, CampaignPage } from './types';

/** Every campaign query sits under this key, so a `campaign.updated` event refreshes them all. */
export const CAMPAIGNS_ROOT_KEY = ['campaigns'] as const;

/** The live campaigns (docs/16 `GET /campaigns`), highest rank first: the first page. */
export function useCampaigns() {
  return useQuery({
    queryKey: [...CAMPAIGNS_ROOT_KEY, 'list'],
    queryFn: () => getJson<CampaignPage>('/campaigns?limit=50'),
  });
}

/** One campaign (docs/16 `GET /campaigns/{id}`): its oldest cases and the entities they share. */
export function useCampaign(id: string) {
  return useQuery({
    queryKey: [...CAMPAIGNS_ROOT_KEY, 'campaign', id],
    queryFn: () => getJson<CampaignDetail>(`/campaigns/${encodeURIComponent(id)}`),
  });
}
