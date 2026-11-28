import { useEffect, useState } from 'react';
import { useParams } from 'react-router';

import {
  useApproveLiens,
  useChase,
  type ChaseCandidate,
  type ChaseLien,
  type ChaseView,
} from '../api/chase';
import { MoneyText } from '../components/MoneyText';
import { ProblemAlert } from '../components/ProblemAlert';
import { RoleGate } from '../components/RoleGate';
import { humanise } from '../lib/format';

const SECOND_MS = 1_000;
const SECONDS_PER_MINUTE = 60;
/** How many characters of an account id stay readable (docs/10 §7: accounts are masked). */
const MASK_KEEPS = 4;

/** An account id on screen: masked to its last characters. */
export function maskAccount(id: string): string {
  return `••••${id.slice(-MASK_KEEPS)}`;
}

/** The golden-hour timer (docs/10 §7): the time left, or that it has ended. */
export function goldenHourLabel(endsAt: string, now: number): string {
  const seconds = Math.ceil((Date.parse(endsAt) - now) / SECOND_MS);
  if (seconds <= 0) return 'Golden hour ended';
  const minutes = Math.floor(seconds / SECONDS_PER_MINUTE);
  const rest = String(seconds % SECONDS_PER_MINUTE).padStart(2, '0');
  return `${String(minutes)} min ${rest} s left`;
}

/** Money a lien is holding now (docs/10 §6): the PLACED liens, not the released or failed ones. */
export function recoveredPaise(liens: readonly ChaseLien[]): number {
  return liens.filter((l) => l.status === 'PLACED').reduce((sum, l) => sum + l.amount_paise, 0);
}

/** A candidate's state: its lien's status once it has one, else what the scoring says. */
export function candidateStatus(candidate: ChaseCandidate, lien: ChaseLien | undefined): string {
  if (lien) return humanise(lien.status);
  if (candidate.recommended === null) return 'Not scored';
  return candidate.recommended ? 'Recommended' : 'Not recommended';
}

function useNow(intervalMs: number): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const id = setInterval(() => {
      setNow(Date.now());
    }, intervalMs);
    return () => {
      clearInterval(id);
    };
  }, [intervalMs]);
  return now;
}

function probability(p: number | null): string {
  return p === null ? '—' : p.toFixed(2);
}

function Money({ paise }: { paise: number | null }) {
  return paise === null ? <>—</> : <MoneyText paise={paise} />;
}

const TH = 'py-2 pr-6 font-normal';
const TD = 'py-3 pr-6';

/** The Chase screen (docs/10 §7, docs/18 §2 `/chase/:id`). */
export function ChasePage() {
  const { id = '' } = useParams();
  const chase = useChase(id);
  return (
    <section aria-labelledby="chase-title" className="max-w-[1320px]">
      <h1 id="chase-title" className="text-[26px] font-semibold tracking-tight">
        Chase
      </h1>
      <p className="mt-1 text-[14px] text-muted">
        Stolen money traced through the accounts it reached, and the liens that hold it.
      </p>
      {chase.isPending && <p className="mt-4 text-[14px] text-muted">Loading the chase…</p>}
      {chase.isError && (
        <div className="mt-4 max-w-xl">
          <ProblemAlert error={chase.error} title="Could not load the chase" />
        </div>
      )}
      {chase.isSuccess && <ChaseDetail chase={chase.data} />}
    </section>
  );
}

function ChaseDetail({ chase }: { chase: ChaseView }) {
  const now = useNow(SECOND_MS);
  const open = chase.status === 'OPEN';
  const recovered = recoveredPaise(chase.liens);
  return (
    <div className="mt-6 space-y-6">
      <div className="grid gap-4 md:grid-cols-3">
        <div className="rounded-md bg-surface px-6 py-4">
          <p className="text-[12px] text-muted">Status</p>
          <p className="mt-1 text-[18px] text-ink">{humanise(chase.status)}</p>
        </div>
        <div className="rounded-md bg-surface px-6 py-4">
          <p className="text-[12px] text-muted">Golden hour</p>
          <p role="timer" className="figure mt-1 text-[18px] text-ink">
            {goldenHourLabel(chase.golden_hour_ends_at, now)}
          </p>
        </div>
        <div className="rounded-md bg-surface px-6 py-4">
          <p id="recovered-label" className="text-[12px] text-muted">
            Recovered of the reported loss
          </p>
          <p className="figure mt-1 text-[18px] text-ink">
            <MoneyText paise={recovered} />{' '}
            <span className="text-[14px] text-muted">
              of <MoneyText paise={chase.reported_loss_paise} />
            </span>
          </p>
          <div
            role="meter"
            aria-labelledby="recovered-label"
            aria-valuemin={0}
            aria-valuemax={chase.reported_loss_paise}
            aria-valuenow={recovered}
            className="mt-2 h-1.5 overflow-hidden rounded-full bg-raised"
          >
            <div
              className="h-full bg-positive"
              style={{
                width: `${String(
                  chase.reported_loss_paise > 0
                    ? Math.min((recovered / chase.reported_loss_paise) * 100, 100)
                    : 0,
                )}%`,
              }}
            />
          </div>
        </div>
      </div>
      <Candidates chase={chase} open={open} />
      <Liens chase={chase} />
    </div>
  );
}

function Candidates({ chase, open }: { chase: ChaseView; open: boolean }) {
  const approve = useApproveLiens(chase.id);
  const liens = new Map(chase.liens.map((l) => [l.candidate_id, l]));
  const free = chase.candidates.filter((c) => !liens.has(c.id));
  // docs/10 §5 step 1: every recommended candidate is selected until the investigator changes it.
  const [picked, setPicked] = useState<ReadonlySet<string> | null>(null);
  const selected = free.filter((c) => (picked ? picked.has(c.id) : c.recommended === true));
  const toggle = (id: string) => {
    const next = new Set(selected.map((c) => c.id));
    if (next.has(id)) next.delete(id);
    else next.add(id);
    setPicked(next);
  };
  if (chase.candidates.length === 0) {
    return (
      <p className="rounded-md border border-dashed border-control px-4 py-3 text-[13px] text-muted">
        No account still holds enough of the traced money to freeze.
      </p>
    );
  }
  return (
    <section aria-labelledby="candidates-title">
      <div className="flex flex-wrap items-end gap-4">
        <h2 id="candidates-title" className="text-[16px] font-semibold">
          Freeze candidates
        </h2>
        <RoleGate permission="chase:write">
          {open && free.length > 0 && (
            <button
              type="button"
              disabled={selected.length === 0 || approve.isPending}
              className="ml-auto rounded-full bg-ink px-4 py-2 text-[13px] font-semibold text-ground hover:bg-soft disabled:opacity-50"
              onClick={() => {
                approve.mutate(
                  selected.map((c) => c.id),
                  {
                    onSuccess: () => {
                      setPicked(null);
                    },
                  },
                );
              }}
            >
              {approve.isPending ? 'Approving…' : `Approve selected (${String(selected.length)})`}
            </button>
          )}
        </RoleGate>
      </div>
      {approve.isError && (
        <div className="mt-3 max-w-xl">
          <ProblemAlert error={approve.error} title="Could not approve the liens" />
        </div>
      )}
      <div className="mt-3 overflow-x-auto rounded-md bg-surface px-8 py-5">
        <table className="w-full min-w-[980px] text-left text-[14px]">
          <caption className="sr-only">Freeze candidates, highest net value first</caption>
          <thead className="text-[12px] text-muted">
            <tr className="border-b border-line">
              <RoleGate permission="chase:write">
                <th scope="col" className={TH}>
                  <span className="sr-only">Select</span>
                </th>
              </RoleGate>
              <th scope="col" className={TH}>
                Account
              </th>
              <th scope="col" className={`${TH} text-right`}>
                Hop
              </th>
              <th scope="col" className={`${TH} text-right`}>
                Traced now
              </th>
              <th scope="col" className={`${TH} text-right`}>
                Still there
              </th>
              <th scope="col" className={`${TH} text-right`}>
                Mule score
              </th>
              <th scope="col" className={`${TH} text-right`}>
                Expected recovery
              </th>
              <th scope="col" className={`${TH} text-right`}>
                Wrongful cost
              </th>
              <th scope="col" className={`${TH} text-right`}>
                Net
              </th>
              <th scope="col" className="py-2 font-normal">
                Status
              </th>
            </tr>
          </thead>
          <tbody>
            {chase.candidates.map((c) => {
              const lien = liens.get(c.id);
              const account = maskAccount(c.account_id);
              return (
                <tr key={c.id} className="border-b border-line last:border-0">
                  <RoleGate permission="chase:write">
                    <td className={TD}>
                      {open && !lien && (
                        <input
                          type="checkbox"
                          aria-label={`Select account ${account}`}
                          className="size-4 accent-[var(--color-accent)]"
                          checked={selected.some((s) => s.id === c.id)}
                          onChange={() => {
                            toggle(c.id);
                          }}
                        />
                      )}
                    </td>
                  </RoleGate>
                  <th scope="row" className={`${TD} font-mono font-normal text-ink`}>
                    {account}
                  </th>
                  <td className={`${TD} figure text-right text-soft`}>{c.hop}</td>
                  <td className={`${TD} figure text-right text-ink`}>
                    <MoneyText paise={c.taint_paise} />
                  </td>
                  <td className={`${TD} figure text-right text-soft`}>
                    {probability(c.p_present)}
                  </td>
                  <td className={`${TD} figure text-right text-soft`}>{probability(c.p_mule)}</td>
                  <td className={`${TD} figure text-right text-soft`}>
                    <Money paise={c.erv_paise} />
                  </td>
                  <td className={`${TD} figure text-right text-soft`}>
                    <Money paise={c.wrongful_cost_paise} />
                  </td>
                  <td
                    className={`${TD} figure text-right ${
                      c.net_paise !== null && c.net_paise < 0 ? 'text-negative' : 'text-ink'
                    }`}
                  >
                    <Money paise={c.net_paise} />
                  </td>
                  <td className="py-3 text-soft">{candidateStatus(c, lien)}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </section>
  );
}

function Liens({ chase }: { chase: ChaseView }) {
  const tickets = new Map(chase.tickets.map((t) => [t.lien_id, t]));
  return (
    <section aria-labelledby="liens-title">
      <h2 id="liens-title" className="text-[16px] font-semibold">
        Liens and tickets
      </h2>
      {chase.liens.length === 0 ? (
        <p className="mt-3 rounded-md border border-dashed border-control px-4 py-3 text-[13px] text-muted">
          No lien yet. A lien exists only once an investigator approves a candidate.
        </p>
      ) : (
        <div className="mt-3 overflow-x-auto rounded-md bg-surface px-8 py-5">
          <table className="w-full min-w-[720px] text-left text-[14px]">
            <caption className="sr-only">Liens, with the ticket sent to another bank</caption>
            <thead className="text-[12px] text-muted">
              <tr className="border-b border-line">
                <th scope="col" className={TH}>
                  Account
                </th>
                <th scope="col" className={TH}>
                  Bank
                </th>
                <th scope="col" className={`${TH} text-right`}>
                  Lien
                </th>
                <th scope="col" className={TH}>
                  Status
                </th>
                <th scope="col" className="py-2 font-normal">
                  Ticket
                </th>
              </tr>
            </thead>
            <tbody>
              {chase.liens.map((lien) => {
                const ticket = tickets.get(lien.id);
                return (
                  <tr key={lien.id} className="border-b border-line last:border-0">
                    <th scope="row" className={`${TD} font-mono font-normal text-ink`}>
                      {maskAccount(lien.account_id)}
                    </th>
                    <td className={`${TD} text-soft`}>{lien.bank_code}</td>
                    <td className={`${TD} figure text-right text-ink`}>
                      <MoneyText paise={lien.amount_paise} />
                    </td>
                    <td className={`${TD} text-soft`}>{humanise(lien.status)}</td>
                    <td className="py-3 text-soft">
                      {ticket ? humanise(ticket.status) : 'None (this bank)'}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}
