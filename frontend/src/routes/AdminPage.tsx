import { useState } from 'react';

import {
  JOB_PAGE_SIZE,
  type JobFilter,
  useGenerateDataset,
  useJobs,
  useRetryJob,
  useTrainSensing,
  useVerifyAudit,
} from '../api/admin';
import { useStatus } from '../api/overview';
import type { DatasetRequest, JobStatus, JobType, JobView, StatusView } from '../api/types';
import { ProblemAlert } from '../components/ProblemAlert';
import { duration, keyText } from '../lib/format';

/**
 * Admin (docs/18 §2 `/admin`, docs/16 §2 "Admin and jobs", #231): is the demo set up, is the
 * audit trail intact, and what are the background jobs doing. The demo story runner and the
 * demo reset arrive with their endpoints (#101, #132).
 */

const PERCENT = 100;

const SELECT =
  'rounded-md border border-control bg-raised px-3 py-1.5 text-[13px] text-ink transition-colors hover:border-soft focus:border-accent focus:outline-none';
const PRIMARY =
  'rounded-full bg-ink px-4 py-1.5 text-[13px] font-semibold text-ground transition-colors hover:bg-soft disabled:opacity-50';
const SECONDARY =
  'rounded-full border border-control px-4 py-1.5 text-[13px] text-soft transition-colors hover:border-soft hover:text-ink disabled:opacity-50';

const MODELS = [
  { kind: 'PRIOR', name: 'Prior model', note: 'The belief before any probe' },
  { kind: 'ANOMALY', name: 'Anomaly model', note: 'How unusual the payment is' },
  { kind: 'MULE', name: 'Mule model', note: 'Whether the payee is a mule' },
] as const;

const PROFILES: readonly { value: DatasetRequest['profile']; label: string }[] = [
  { value: 'demo', label: 'Demo' },
  { value: 'eval', label: 'Evaluation' },
  { value: 'test', label: 'Test (small)' },
];

const STATUSES: readonly JobStatus[] = ['QUEUED', 'RUNNING', 'SUCCEEDED', 'FAILED', 'CANCELLED'];

/** The job types an operator starts or watches; the sweeps are listed after them. */
const TYPES: readonly JobType[] = [
  'GENERATE_DATASET',
  'TRAIN_SENSING',
  'RUN_EVALUATION',
  'RUN_LEARNING',
  'RUN_SCOUT',
  'RUN_BACKTEST',
  'DEMO_STORY',
  'DEMO_RESET',
  'LLM_NARRATIVE',
  'LLM_CASE_SUMMARY',
  'SIM_OUTREACH_CALL',
  'SIM_TICK',
  'CHASE_RETRACE',
  'MULE_SWEEP',
  'SESSION_TIMEOUT_SWEEP',
  'RETENTION_SWEEP',
  'AUDIT_VERIFY',
];

const STATUS_TONE: Readonly<Record<JobStatus, string>> = {
  QUEUED: 'text-soft',
  RUNNING: 'text-accent',
  SUCCEEDED: 'text-positive',
  FAILED: 'text-negative',
  CANCELLED: 'text-muted',
};

const WHEN = new Intl.DateTimeFormat('en-IN', {
  day: 'numeric',
  month: 'short',
  hour: '2-digit',
  minute: '2-digit',
  hourCycle: 'h23',
});

const COUNT = new Intl.NumberFormat('en-IN');

export function AdminPage() {
  const status = useStatus();
  return (
    <section aria-labelledby="admin-title" className="max-w-[1320px]">
      <h1 id="admin-title" className="text-[26px] font-semibold tracking-tight">
        Admin
      </h1>
      <p className="mt-1 text-[14px] text-muted">
        {status.data
          ? readiness(status.data)
          : 'Whether the demo is set up, and what the jobs are doing.'}
      </p>

      {status.isError && (
        <div className="mt-6 max-w-xl">
          <ProblemAlert error={status.error} title="Could not load the readiness" />
        </div>
      )}
      <Readiness status={status.data} />

      <div className="mt-6 grid gap-6 lg:grid-cols-2">
        <Setup status={status.data} />
        <AuditChain />
      </div>

      <Jobs />
    </section>
  );
}

function readiness(status: StatusView): string {
  if (!status.dataset_ready) return 'The demo has no dataset yet: generate one to start.';
  if (!status.models_ready) return 'The dataset is ready; the sensing models are still training.';
  if (status.models_below_gate.length > 0) {
    return 'DELPHI is running, with a model below its quality gate.';
  }
  return 'DELPHI is set up: the dataset and every sensing model are ready.';
}

function Cell({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="min-w-0 px-6 py-5 first:pl-0 sm:border-l sm:border-line sm:first:border-l-0">
      <dt className="text-[12px] text-muted">{label}</dt>
      <dd className="mt-2 text-[17px] leading-tight">{children}</dd>
    </div>
  );
}

function Readiness({ status }: { status: StatusView | undefined }) {
  const seeding =
    status && !status.dataset_ready && status.seed_progress !== null
      ? ` ${(status.seed_progress * PERCENT).toFixed(0)}%`
      : '';
  return (
    <dl
      aria-label="Readiness"
      className="mt-6 grid grid-cols-2 rounded-md bg-surface px-8 md:grid-cols-4"
    >
      <Cell label="Dataset">
        {!status ? (
          <span className="text-muted">—</span>
        ) : status.dataset_ready ? (
          <span className="text-positive">Ready</span>
        ) : (
          <span className="text-warning">Not ready{seeding}</span>
        )}
      </Cell>
      {MODELS.map((m) => {
        const model = status?.models[m.kind];
        const belowGate = status?.models_below_gate.includes(m.kind) ?? false;
        return (
          <Cell key={m.kind} label={m.name}>
            {!status ? (
              <span className="text-muted">—</span>
            ) : model ? (
              <>
                <span className={belowGate ? 'text-warning' : 'text-ink'}>
                  v<span className="figure">{model.version}</span>
                </span>
                <span className={`ml-2 text-[12px] ${belowGate ? 'text-warning' : 'text-muted'}`}>
                  {belowGate ? 'below its gate' : 'gate passed'}
                </span>
              </>
            ) : (
              <span className="text-warning">Not trained</span>
            )}
            <span className="mt-1 block text-[12px] text-muted">{m.note}</span>
          </Cell>
        );
      })}
    </dl>
  );
}

function Card({
  id,
  title,
  note,
  action,
  children,
}: {
  id: string;
  title: string;
  note?: string;
  action?: React.ReactNode;
  children: React.ReactNode;
}) {
  return (
    <section aria-labelledby={id} className="min-w-0 rounded-md bg-surface p-8">
      <div className="flex flex-wrap items-baseline justify-between gap-4">
        <h2 id={id} className="text-[20px] text-muted">
          {title}
        </h2>
        {action}
      </div>
      {note && <p className="mt-1 max-w-[70ch] text-[13px] text-muted">{note}</p>}
      <div className="mt-5">{children}</div>
    </section>
  );
}

function Started({ job }: { job: JobView | undefined }) {
  if (!job) return null;
  return (
    <p role="status" className="mt-3 text-[13px] text-soft">
      Queued: {keyText(job.type)}. Follow it in the jobs below.
    </p>
  );
}

function Setup({ status }: { status: StatusView | undefined }) {
  const [profile, setProfile] = useState<DatasetRequest['profile']>('demo');
  const generate = useGenerateDataset();
  const train = useTrainSensing();
  const hasDataset = status?.dataset_ready ?? false;
  return (
    <Card
      id="setup-title"
      title="Set up the demo"
      note="Generating the dataset makes the simulated bank: its customers, payees and history. The sensing models train on it."
    >
      <div className="space-y-6">
        <div>
          <h3 className="text-[14px] text-ink">The dataset</h3>
          <div className="mt-2 flex flex-wrap items-center gap-3">
            <label className="sr-only" htmlFor="dataset-profile">
              Profile
            </label>
            <select
              id="dataset-profile"
              className={SELECT}
              value={profile}
              onChange={(e) => {
                setProfile(e.target.value as DatasetRequest['profile']);
              }}
            >
              {PROFILES.map((p) => (
                <option key={p.value} value={p.value}>
                  {p.label}
                </option>
              ))}
            </select>
            <button
              type="button"
              className={hasDataset ? SECONDARY : PRIMARY}
              disabled={generate.isPending}
              onClick={() => {
                generate.mutate({ profile });
              }}
            >
              Generate the dataset
            </button>
          </div>
          {hasDataset && (
            <p className="mt-2 text-[12px] text-muted">
              The bank already holds a dataset, so the server will refuse another.
            </p>
          )}
          {generate.isError && (
            <div className="mt-3">
              <ProblemAlert error={generate.error} title="Could not generate the dataset" />
            </div>
          )}
          <Started job={generate.data} />
        </div>
        <div>
          <h3 className="text-[14px] text-ink">The sensing models</h3>
          <p className="mt-1 text-[12px] text-muted">
            A model that fails its quality gate is kept aside; the active one stays.
          </p>
          <button
            type="button"
            className={`mt-2 ${SECONDARY}`}
            disabled={train.isPending || !hasDataset}
            onClick={() => {
              train.mutate(undefined);
            }}
          >
            Retrain the models
          </button>
          {train.isError && (
            <div className="mt-3">
              <ProblemAlert error={train.error} title="Could not start the training" />
            </div>
          )}
          <Started job={train.data} />
        </div>
      </div>
    </Card>
  );
}

function AuditChain() {
  const verify = useVerifyAudit();
  const result = verify.data;
  return (
    <Card
      id="audit-title"
      title="Audit chain"
      note="Every state change writes an audit entry hashed with the one before it, so an edited or deleted entry breaks the chain. Checking reads every entry."
    >
      <div className="flex flex-wrap items-end gap-x-8 gap-y-4">
        <div aria-live="polite" className="min-w-0">
          {result ? (
            result.ok ? (
              <>
                <p className="text-[24px] leading-none text-positive">Intact</p>
                <p className="mt-2 text-[13px] text-muted">
                  <span className="figure text-soft">{COUNT.format(result.checked)}</span> entries
                  checked
                </p>
              </>
            ) : (
              <>
                <p className="text-[24px] leading-none text-negative">Broken</p>
                <p className="mt-2 text-[13px] text-muted">
                  First bad entry:{' '}
                  <span className="figure text-negative">#{result.first_bad_id ?? '?'}</span>, after{' '}
                  <span className="figure">{COUNT.format(result.checked)}</span> checked
                </p>
              </>
            )
          ) : (
            <p className="text-[24px] leading-none text-muted">Not checked</p>
          )}
        </div>
        <button
          type="button"
          className={SECONDARY}
          disabled={verify.isPending}
          onClick={() => {
            verify.mutate();
          }}
        >
          {verify.isPending ? 'Checking…' : result ? 'Check again' : 'Check the chain'}
        </button>
      </div>
      {verify.isError && (
        <div className="mt-4">
          <ProblemAlert error={verify.error} title="Could not check the chain" />
        </div>
      )}
    </Card>
  );
}

function Jobs() {
  const [filter, setFilter] = useState<JobFilter>({});
  const [limit, setLimit] = useState(JOB_PAGE_SIZE);
  const jobs = useJobs(filter, limit);
  const retry = useRetryJob();
  const items = jobs.data?.items ?? [];
  return (
    <div className="mt-6">
      <Card
        id="jobs-title"
        title="Jobs"
        note="The background work: generating, training, evaluating, sweeping. A job is tried up to three times; a failed one can be retried."
        action={
          <div className="flex flex-wrap gap-3">
            <label className="sr-only" htmlFor="job-status">
              Status
            </label>
            <select
              id="job-status"
              className={SELECT}
              value={filter.status ?? ''}
              onChange={(e) => {
                const value = e.target.value as JobStatus | '';
                setFilter((f) => ({ ...f, status: value || undefined }));
                setLimit(JOB_PAGE_SIZE);
              }}
            >
              <option value="">Any status</option>
              {STATUSES.map((s) => (
                <option key={s} value={s}>
                  {keyText(s)}
                </option>
              ))}
            </select>
            <label className="sr-only" htmlFor="job-type">
              Type
            </label>
            <select
              id="job-type"
              className={SELECT}
              value={filter.type ?? ''}
              onChange={(e) => {
                const value = e.target.value as JobType | '';
                setFilter((f) => ({ ...f, type: value || undefined }));
                setLimit(JOB_PAGE_SIZE);
              }}
            >
              <option value="">Any type</option>
              {TYPES.map((t) => (
                <option key={t} value={t}>
                  {keyText(t)}
                </option>
              ))}
            </select>
          </div>
        }
      >
        {jobs.isPending && <p className="text-[14px] text-muted">Loading the jobs…</p>}
        {jobs.isError && <ProblemAlert error={jobs.error} title="Could not load the jobs" />}
        {retry.isError && (
          <div className="mb-4">
            <ProblemAlert error={retry.error} title="Could not retry the job" />
          </div>
        )}
        {jobs.isSuccess && items.length === 0 && (
          <p className="rounded-md border border-dashed border-control px-4 py-3 text-[13px] text-muted">
            No jobs match.
          </p>
        )}
        {items.length > 0 && (
          <div className="overflow-x-auto">
            <table className="w-full min-w-[760px] text-left text-[14px]">
              <caption className="sr-only">Jobs, newest first</caption>
              <thead className="text-[12px] text-muted">
                <tr className="border-b border-line">
                  {['Job', 'Status', 'Progress', 'Tries', 'Queued', 'Took', ''].map((h, i) => (
                    <th key={i} scope="col" className="py-2 pr-6 font-normal">
                      {h}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {items.map((job) => (
                  <JobRow
                    key={job.id}
                    job={job}
                    retrying={retry.isPending && retry.variables === job.id}
                    onRetry={() => {
                      retry.mutate(job.id);
                    }}
                  />
                ))}
              </tbody>
            </table>
          </div>
        )}
        {jobs.data?.next_cursor && (
          <button
            type="button"
            className={`mt-5 ${SECONDARY}`}
            disabled={jobs.isFetching}
            onClick={() => {
              setLimit((n) => n + JOB_PAGE_SIZE);
            }}
          >
            Show more
          </button>
        )}
      </Card>
    </div>
  );
}

function JobRow({
  job,
  retrying,
  onRetry,
}: {
  job: JobView;
  retrying: boolean;
  onRetry: () => void;
}) {
  const took = job.finished_at
    ? (Date.parse(job.finished_at) - Date.parse(job.created_at)) / 1000
    : null;
  const progress = Math.min(Math.max(job.progress, 0), 1);
  return (
    <tr className="border-b border-line align-top last:border-0">
      <td className="py-3 pr-6">
        <span className="text-ink">{keyText(job.type)}</span>
        {job.error && (
          <span className="mt-1 block max-w-[52ch] text-[12px] break-words text-negative">
            {job.error}
          </span>
        )}
      </td>
      <td className={`py-3 pr-6 whitespace-nowrap ${STATUS_TONE[job.status]}`}>
        {job.status === 'RUNNING' && (
          <span aria-hidden className="relative mr-2 inline-flex size-2 align-middle">
            <span className="absolute inline-flex size-full animate-ping rounded-full bg-accent opacity-60" />
            <span className="relative inline-flex size-2 rounded-full bg-accent" />
          </span>
        )}
        {keyText(job.status)}
      </td>
      <td className="py-3 pr-6">
        <div className="flex items-center gap-3">
          <div className="h-1 w-24 rounded-full bg-raised">
            <div
              className={`h-full rounded-full transition-[width] duration-500 ${
                job.status === 'FAILED'
                  ? 'bg-negative'
                  : job.status === 'SUCCEEDED'
                    ? 'bg-positive'
                    : 'bg-accent'
              }`}
              style={{ width: `${String(progress * PERCENT)}%` }}
            />
          </div>
          <span className="figure text-[12px] text-muted">{(progress * PERCENT).toFixed(0)}%</span>
        </div>
      </td>
      <td className="figure py-3 pr-6 text-soft">{job.attempts}</td>
      <td className="py-3 pr-6 whitespace-nowrap text-soft">
        {WHEN.format(new Date(job.created_at))}
      </td>
      <td className="py-3 pr-6 whitespace-nowrap text-soft">
        {took !== null && took < 1 ? 'under 1 s' : duration(took)}
      </td>
      <td className="py-3 text-right">
        {job.status === 'FAILED' && (
          <button
            type="button"
            className={SECONDARY}
            disabled={retrying}
            aria-label={`Retry ${keyText(job.type)}`}
            onClick={onRetry}
          >
            Retry
          </button>
        )}
      </td>
    </tr>
  );
}
