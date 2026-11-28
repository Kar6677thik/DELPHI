import { Link } from 'react-router';

import { useCampaigns } from '../api/campaigns';
import { F3Strip } from '../components/F3Strip';
import { MoneyText } from '../components/MoneyText';
import { ProblemAlert } from '../components/ProblemAlert';
import { keyText } from '../lib/format';

/**
 * The campaigns (docs/18 §2 `/campaigns`, docs/09 §8): the live campaigns, highest
 * `rank_score` first, with the F3 stage strip, the case count, the total expected loss and the
 * vulnerable victims. A campaign is derived from its cases (docs/05 §5), so there is nothing to
 * write here; a recompute refreshes the list (`campaign.updated`).
 */
export function CampaignsPage() {
  const campaigns = useCampaigns();
  const items = campaigns.data?.pages.flatMap((page) => page.items) ?? [];
  return (
    <section aria-labelledby="campaigns-title" className="max-w-[1320px]">
      <h1 id="campaigns-title" className="text-[26px] font-semibold tracking-tight">
        Campaigns
      </h1>
      <p className="mt-1 text-[14px] text-muted">
        Cases that share a device, a counterparty account or a login network, with the same leading
        hypothesis, within a week of each other. Highest ranked first.
      </p>
      <div className="mt-6">
        {campaigns.isPending && <p className="text-[14px] text-muted">Loading campaigns…</p>}
        {campaigns.isError && (
          <ProblemAlert error={campaigns.error} title="Could not load the campaigns" />
        )}
        {campaigns.isSuccess && items.length === 0 && (
          <p className="rounded-md border border-dashed border-control px-4 py-3 text-[13px] text-muted">
            No campaigns yet. They form when two or more cases share infrastructure.
          </p>
        )}
        {campaigns.isSuccess && items.length > 0 && (
          <div className="overflow-x-auto rounded-md bg-surface px-8 py-5">
            <table className="w-full min-w-[860px] text-left text-[14px]">
              <caption className="sr-only">Campaigns, highest ranked first</caption>
              <thead className="text-[12px] text-muted">
                <tr className="border-b border-line">
                  <th scope="col" className="py-2 pr-6 font-normal">
                    Leading hypothesis
                  </th>
                  <th scope="col" className="py-2 pr-6 font-normal">
                    F3 stage
                  </th>
                  <th scope="col" className="py-2 pr-6 text-right font-normal">
                    Cases
                  </th>
                  <th scope="col" className="py-2 pr-6 text-right font-normal">
                    Expected loss
                  </th>
                  <th scope="col" className="py-2 text-right font-normal">
                    Vulnerable victims
                  </th>
                </tr>
              </thead>
              <tbody>
                {items.map((c) => (
                  <tr key={c.id} className="border-b border-line last:border-0">
                    <td className="py-3 pr-6">
                      <Link
                        to={`/campaigns/${c.id}`}
                        className="text-ink underline decoration-control underline-offset-4 transition-colors hover:decoration-soft"
                      >
                        {keyText(c.leading_hypothesis)}
                      </Link>
                    </td>
                    <td className="py-3 pr-6">
                      <F3Strip furthest={c.f3_stage_max} compact />
                    </td>
                    <td className="figure py-3 pr-6 text-right text-ink">{c.case_count}</td>
                    <td className="figure py-3 pr-6 text-right text-ink">
                      <MoneyText paise={c.expected_loss_paise} />
                    </td>
                    <td className="figure py-3 text-right text-soft">{c.vulnerable_victims}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
        {campaigns.hasNextPage && (
          <button
            type="button"
            className="mt-4 rounded-full border border-control px-4 py-1.5 text-[13px] text-soft transition-colors hover:border-soft hover:text-ink disabled:opacity-50"
            disabled={campaigns.isFetchingNextPage}
            onClick={() => {
              void campaigns.fetchNextPage();
            }}
          >
            {campaigns.isFetchingNextPage ? 'Loading…' : 'Load more'}
          </button>
        )}
      </div>
    </section>
  );
}
