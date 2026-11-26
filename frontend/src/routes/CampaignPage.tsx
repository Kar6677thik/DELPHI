import { Link, useParams } from 'react-router';

import { useCampaign } from '../api/campaigns';
import type { SharedEntity } from '../api/types';
import { F3Strip } from '../components/F3Strip';
import { MoneyText } from '../components/MoneyText';
import { ProblemAlert } from '../components/ProblemAlert';
import { humanise, keyText } from '../lib/format';

const ENTITY_KINDS: Readonly<Record<string, string>> = {
  account: 'Counterparty account',
  device: 'Device fingerprint',
  ip24: 'Login network (/24)',
};

/** An entity's kind in words: `ip24` → "Login network (/24)". */
export function entityKind(entity: SharedEntity): string {
  return ENTITY_KINDS[entity.kind] ?? humanise(entity.kind);
}

const LINK =
  'text-ink underline decoration-control underline-offset-4 transition-colors hover:decoration-soft';

/**
 * One campaign (docs/18 §2 `/campaigns/:id`, docs/09 §8): the F3 stage strip, its totals, the
 * infrastructure its cases share and the cases themselves (the oldest 200; the case queue,
 * filtered to the campaign, has all of them). A campaign that merged points to the one that
 * took its cases.
 */
export function CampaignPage() {
  const { id = '' } = useParams();
  const campaign = useCampaign(id);
  return (
    <section aria-labelledby="campaign-title" className="max-w-[1320px]">
      <p className="text-[13px] text-muted">
        <Link to="/campaigns" className="underline">
          Campaigns
        </Link>
      </p>
      <h1 id="campaign-title" className="mt-1 text-[26px] font-semibold tracking-tight">
        {campaign.isSuccess ? keyText(campaign.data.leading_hypothesis) : 'Campaign'}
      </h1>
      {campaign.isPending && <p className="mt-4 text-[14px] text-muted">Loading the campaign…</p>}
      {campaign.isError && (
        <div className="mt-4">
          <ProblemAlert error={campaign.error} title="Could not load the campaign" />
        </div>
      )}
      {campaign.isSuccess && (
        <>
          {campaign.data.merged_into !== null && (
            <p
              role="status"
              className="mt-3 rounded-md border border-control px-4 py-3 text-[13px] text-soft"
            >
              This campaign merged into an older one, which now holds its cases:{' '}
              <Link to={`/campaigns/${campaign.data.merged_into}`} className={LINK}>
                open that campaign
              </Link>
              .
            </p>
          )}
          <div className="mt-4">
            <F3Strip furthest={campaign.data.f3_stage_max} />
          </div>
          <dl className="mt-6 grid grid-cols-2 gap-4 sm:grid-cols-4">
            {[
              ['Cases', String(campaign.data.case_count)],
              ['Vulnerable victims', String(campaign.data.vulnerable_victims)],
            ].map(([label, value]) => (
              <div key={label} className="rounded-md bg-surface px-4 py-3">
                <dt className="text-[12px] text-muted">{label}</dt>
                <dd className="figure mt-1 text-[20px] text-ink">{value}</dd>
              </div>
            ))}
            <div className="rounded-md bg-surface px-4 py-3">
              <dt className="text-[12px] text-muted">Expected loss</dt>
              <dd className="figure mt-1 text-[20px] text-ink">
                <MoneyText paise={campaign.data.expected_loss_paise} />
              </dd>
            </div>
            <div className="rounded-md bg-surface px-4 py-3">
              <dt className="text-[12px] text-muted">Rank score</dt>
              <dd className="figure mt-1 text-[20px] text-ink">
                {Math.round(campaign.data.rank_score).toLocaleString('en-IN')}
              </dd>
            </div>
          </dl>

          <h2 className="mt-8 text-[18px] font-semibold">Shared infrastructure</h2>
          {campaign.data.shared_entities.length === 0 ? (
            <p className="mt-2 text-[13px] text-muted">
              No entity is shared by two or more of its cases now.
            </p>
          ) : (
            <div className="mt-3 overflow-x-auto rounded-md bg-surface px-8 py-5">
              <table className="w-full text-left text-[14px]">
                <caption className="sr-only">Entities two or more of its cases share</caption>
                <thead className="text-[12px] text-muted">
                  <tr className="border-b border-line">
                    <th scope="col" className="py-2 pr-6 font-normal">
                      Kind
                    </th>
                    <th scope="col" className="py-2 pr-6 font-normal">
                      Value
                    </th>
                    <th scope="col" className="py-2 text-right font-normal">
                      Cases
                    </th>
                  </tr>
                </thead>
                <tbody>
                  {campaign.data.shared_entities.map((e) => (
                    <tr key={`${e.kind}:${e.value}`} className="border-b border-line last:border-0">
                      <td className="py-3 pr-6 text-soft">{entityKind(e)}</td>
                      <td className="py-3 pr-6 font-mono text-[13px] break-all text-ink">
                        {e.value}
                      </td>
                      <td className="figure py-3 text-right text-ink">{e.case_count}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}

          <h2 className="mt-8 text-[18px] font-semibold">Cases</h2>
          {campaign.data.cases.length === 0 ? (
            <p className="mt-2 text-[13px] text-muted">No case is in this campaign now.</p>
          ) : (
            <div className="mt-3 overflow-x-auto rounded-md bg-surface px-8 py-5">
              <table className="w-full min-w-[640px] text-left text-[14px]">
                <caption className="sr-only">The campaign's cases, oldest first</caption>
                <thead className="text-[12px] text-muted">
                  <tr className="border-b border-line">
                    <th scope="col" className="py-2 pr-6 font-normal">
                      Opened
                    </th>
                    <th scope="col" className="py-2 pr-6 font-normal">
                      Type
                    </th>
                    <th scope="col" className="py-2 pr-6 text-right font-normal">
                      At stake
                    </th>
                    <th scope="col" className="py-2 font-normal">
                      Status
                    </th>
                  </tr>
                </thead>
                <tbody>
                  {campaign.data.cases.map((c) => (
                    <tr key={c.id} className="border-b border-line last:border-0">
                      <td className="py-3 pr-6">
                        <Link to={`/cases/${c.id}`} className={LINK}>
                          {new Date(c.opened_at).toLocaleString('en-IN')}
                        </Link>
                      </td>
                      <td className="py-3 pr-6 text-soft">{humanise(c.type)}</td>
                      <td className="figure py-3 pr-6 text-right text-ink">
                        <MoneyText paise={c.amount_at_stake_paise} />
                      </td>
                      <td className="py-3 text-soft">{humanise(c.status)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
              {campaign.data.case_count > campaign.data.cases.length && (
                <p className="mt-3 text-[12px] text-muted">
                  The oldest {campaign.data.cases.length} of {campaign.data.case_count}.{' '}
                  <Link to={`/cases?campaign_id=${campaign.data.id}`} className={LINK}>
                    All of them in the case queue
                  </Link>
                </p>
              )}
            </div>
          )}
        </>
      )}
    </section>
  );
}
