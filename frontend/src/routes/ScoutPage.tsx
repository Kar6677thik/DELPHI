import { Link } from 'react-router';

import { QUEUE_PAGE, useClusters, useRunScout, useScoutQueue } from '../api/scout';
import type { ClusterView } from '../api/types';
import { ProblemAlert } from '../components/ProblemAlert';
import { RoleGate } from '../components/RoleGate';
import { humanise } from '../lib/format';

/**
 * The Typology Scout (docs/18 §2 `/scout`, docs/11, #118): how many unexplained items wait, a
 * button to cluster them now (`RUN_SCOUT`), and the clusters found, each with the features that
 * set it apart and its draft. Opening a draft leads to its editor.
 */

const PRIMARY =
  'rounded-full bg-ink px-4 py-1.5 text-[13px] font-semibold text-ground transition-colors hover:bg-soft disabled:opacity-50';
const DATE = new Intl.DateTimeFormat('en-IN', { dateStyle: 'medium', timeZone: 'UTC' });
const TOP_FEATURES = 3;

interface Feature {
  feature: string;
  separates: boolean;
  cluster_mean: number;
  reference_mean: number;
}

function isFeature(value: unknown): value is Feature {
  if (typeof value !== 'object' || value === null) return false;
  const f = value as Record<string, unknown>;
  return (
    typeof f.feature === 'string' &&
    typeof f.cluster_mean === 'number' &&
    typeof f.reference_mean === 'number'
  );
}

/** The cluster's stored features (`distinguishing_features.features`), checked, not cast. */
export function clusterFeatures(cluster: ClusterView): Feature[] {
  const raw = (cluster.distinguishing_features as { features?: unknown }).features;
  return Array.isArray(raw) ? raw.filter(isFeature) : [];
}

function figure(value: number): string {
  return Number.isInteger(value) ? String(value) : value.toFixed(2);
}

export function ScoutPage() {
  const queue = useScoutQueue();
  const clusters = useClusters();
  const run = useRunScout();
  const waiting = queue.data?.items.length ?? 0;
  const more = queue.data?.next_cursor != null;
  return (
    <section aria-labelledby="scout-title" className="max-w-[1320px]">
      <h1 id="scout-title" className="text-[26px] font-semibold tracking-tight">
        Typology Scout
      </h1>
      <p className="mt-1 max-w-[80ch] text-[14px] text-muted">
        Payments no known typology explains are grouped by what they share. Each group becomes a
        draft typology an engineer edits, backtests and, after shadow evaluation, promotes.
      </p>

      <div className="mt-6 flex flex-wrap items-center gap-6 rounded-md bg-surface p-6">
        <div>
          <p className="text-[12px] text-muted">Unexplained items waiting</p>
          <p className="figure mt-1 text-[24px]">
            {queue.isSuccess ? `${String(waiting)}${more ? '+' : ''}` : '—'}
          </p>
          {more && (
            <p className="text-[11px] text-muted">
              More than {String(QUEUE_PAGE)}; a run takes the newest.
            </p>
          )}
        </div>
        <RoleGate permission="fde">
          <button
            type="button"
            className={PRIMARY}
            disabled={run.isPending}
            onClick={() => {
              run.mutate(undefined);
            }}
          >
            Run the Scout now
          </button>
        </RoleGate>
        {run.isSuccess && (
          <p role="status" className="text-[13px] text-soft">
            {run.data.created
              ? 'Run started. The clusters appear below when it ends.'
              : 'A run is already under way.'}
          </p>
        )}
      </div>
      {queue.isError && (
        <div className="mt-3">
          <ProblemAlert error={queue.error} title="Could not load the queue" />
        </div>
      )}
      {run.isError && (
        <div className="mt-3">
          <ProblemAlert error={run.error} title="Could not start the run" />
        </div>
      )}

      <h2 className="mt-8 text-[20px] text-muted">Clusters</h2>
      {clusters.isError && (
        <div className="mt-3">
          <ProblemAlert error={clusters.error} title="Could not load the clusters" />
        </div>
      )}
      {clusters.isPending && <p className="mt-3 text-[13px] text-muted">Loading the clusters…</p>}
      {clusters.isSuccess && clusters.data.items.length === 0 && (
        <p className="mt-3 text-[13px] text-muted">No cluster yet. A run groups the queue.</p>
      )}
      {clusters.isSuccess && clusters.data.items.length > 0 && (
        <table className="mt-3 w-full text-left text-[13px]">
          <caption className="sr-only">Clusters, newest first</caption>
          <thead className="text-[12px] text-muted">
            <tr>
              <th scope="col" className="py-2 pr-4 font-normal">
                Suggested name
              </th>
              <th scope="col" className="py-2 pr-4 font-normal">
                Items
              </th>
              <th scope="col" className="py-2 pr-4 font-normal">
                What sets it apart (cluster vs the rest)
              </th>
              <th scope="col" className="py-2 pr-4 font-normal">
                Found
              </th>
              <th scope="col" className="py-2 font-normal">
                Draft
              </th>
            </tr>
          </thead>
          <tbody>
            {clusters.data.items.map((c) => (
              <tr key={c.id} className="border-t border-line align-top">
                <td className="py-3 pr-4">{c.suggested_name ?? 'Unnamed cluster'}</td>
                <td className="figure py-3 pr-4">{c.size}</td>
                <td className="py-3 pr-4">
                  <ul className="space-y-0.5">
                    {clusterFeatures(c)
                      .slice(0, TOP_FEATURES)
                      .map((f) => (
                        <li key={f.feature}>
                          {humanise(f.feature)}:{' '}
                          <span className="figure">{figure(f.cluster_mean)}</span>
                          <span className="text-muted">
                            {' '}
                            vs <span className="figure">{figure(f.reference_mean)}</span>
                          </span>
                        </li>
                      ))}
                  </ul>
                </td>
                <td className="py-3 pr-4 text-soft">{DATE.format(new Date(c.created_at))}</td>
                <td className="py-3">
                  {c.draft_id ? (
                    <Link
                      to={`/scout/drafts/${c.draft_id}`}
                      className="text-accent underline-offset-2 hover:underline"
                    >
                      {humanise(c.draft_status ?? 'draft')}
                      <span className="sr-only">: open the draft of {c.suggested_name}</span>
                    </Link>
                  ) : (
                    <span className="text-muted">—</span>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </section>
  );
}
