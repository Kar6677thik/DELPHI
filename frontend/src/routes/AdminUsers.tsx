import { type FormEvent, Fragment, useState } from 'react';

import { MIN_PASSWORD_LENGTH, useCreateUser, useUpdateUser, useUsers } from '../api/admin';
import type { Role, UserView } from '../api/types';
import { ProblemAlert } from '../components/ProblemAlert';
import { useSession } from '../lib/session';

/**
 * Users (docs/16 §2 "Admin and jobs", FR-PLT-10, #231): create, change role, disable, reset a
 * password. Every change ends the user's sessions on the server, so it takes effect at once.
 */

const ROLES: readonly { value: Role; label: string; note: string }[] = [
  { value: 'VIEWER', label: 'Viewer', note: 'Reads every screen' },
  { value: 'INVESTIGATOR', label: 'Investigator', note: 'Works cases and chases' },
  { value: 'FDE_ENGINEER', label: 'FDE engineer', note: 'Edits typologies and the KB' },
  { value: 'ADMIN', label: 'Admin', note: 'Everything, and this screen' },
];

const FIELD =
  'w-full rounded-md border border-control bg-raised px-3 py-1.5 text-[14px] text-ink transition-colors hover:border-soft focus:border-accent focus:outline-none';
const SELECT =
  'rounded-md border border-control bg-raised px-2.5 py-1 text-[13px] text-ink transition-colors hover:border-soft focus:border-accent focus:outline-none disabled:opacity-50';
const PRIMARY =
  'rounded-full bg-ink px-4 py-1.5 text-[13px] font-semibold text-ground transition-colors hover:bg-soft disabled:opacity-50';
const SECONDARY =
  'rounded-full border border-control px-3 py-1 text-[13px] text-soft transition-colors hover:border-soft hover:text-ink disabled:opacity-50';

const ADDED = new Intl.DateTimeFormat('en-IN', { dateStyle: 'medium' });

export function Users() {
  const users = useUsers();
  const update = useUpdateUser();
  const me = useSession();
  const [adding, setAdding] = useState(false);
  const [resetting, setResetting] = useState<string | null>(null);
  const list = users.data ?? [];
  const enabled = list.filter((u) => !u.disabled).length;

  return (
    <section aria-labelledby="users-title" className="mt-6 min-w-0 rounded-md bg-surface p-8">
      <div className="flex flex-wrap items-baseline justify-between gap-4">
        <h2 id="users-title" className="text-[20px] text-muted">
          Users
        </h2>
        {!adding && (
          <button
            type="button"
            className={SECONDARY}
            onClick={() => {
              setAdding(true);
            }}
          >
            Add a user
          </button>
        )}
      </div>
      <p className="mt-1 max-w-[70ch] text-[13px] text-muted">
        {users.data ? `${String(list.length)} accounts, ${String(enabled)} enabled. ` : ''}A change
        of role, a disable or a new password signs that person out everywhere.
      </p>

      <div className="mt-5 space-y-5">
        {adding && (
          <AddUser
            onDone={() => {
              setAdding(false);
            }}
          />
        )}
        {users.isPending && <p className="text-[14px] text-muted">Loading the users…</p>}
        {users.isError && <ProblemAlert error={users.error} title="Could not load the users" />}
        {update.isError && <ProblemAlert error={update.error} title="Could not change the user" />}
        {list.length > 0 && (
          <div className="overflow-x-auto">
            <table className="w-full min-w-[720px] text-left text-[14px]">
              <caption className="sr-only">Users</caption>
              <thead className="text-[12px] text-muted">
                <tr className="border-b border-line">
                  {['Name', 'Role', 'Status', 'Added', ''].map((h, i) => (
                    <th key={i} scope="col" className="py-2 pr-6 font-normal">
                      {h}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {list.map((user) => {
                  const self = me?.username === user.username;
                  const busy = update.isPending && update.variables.user.id === user.id;
                  return (
                    <Fragment key={user.id}>
                      <tr className="border-b border-line align-middle">
                        <td className="py-3 pr-6">
                          <span className={user.disabled ? 'text-muted' : 'text-ink'}>
                            {user.display_name}
                          </span>
                          {self && (
                            <span className="ml-2 rounded-sm bg-raised px-1.5 py-0.5 text-[11px] text-soft">
                              you
                            </span>
                          )}
                          <span className="block text-[12px] text-muted">{user.username}</span>
                        </td>
                        <td className="py-3 pr-6">
                          <label className="sr-only" htmlFor={`role-${user.id}`}>
                            Role of {user.display_name}
                          </label>
                          <select
                            id={`role-${user.id}`}
                            className={SELECT}
                            value={user.role}
                            disabled={busy}
                            onChange={(e) => {
                              update.mutate({ user, change: { role: e.target.value as Role } });
                            }}
                          >
                            {ROLES.map((r) => (
                              <option key={r.value} value={r.value}>
                                {r.label}
                              </option>
                            ))}
                          </select>
                        </td>
                        <td
                          className={`py-3 pr-6 ${user.disabled ? 'text-muted' : 'text-positive'}`}
                        >
                          {user.disabled ? 'Disabled' : 'Active'}
                        </td>
                        <td className="py-3 pr-6 whitespace-nowrap text-soft">
                          {ADDED.format(new Date(user.created_at))}
                        </td>
                        <td className="py-3 text-right whitespace-nowrap">
                          <span className="inline-flex gap-2">
                            <button
                              type="button"
                              className={SECONDARY}
                              aria-expanded={resetting === user.id}
                              onClick={() => {
                                setResetting(resetting === user.id ? null : user.id);
                              }}
                            >
                              Reset password
                            </button>
                            {!self && (
                              <button
                                type="button"
                                className={SECONDARY}
                                disabled={busy}
                                onClick={() => {
                                  update.mutate({ user, change: { disabled: !user.disabled } });
                                }}
                              >
                                {user.disabled ? 'Enable' : 'Disable'}
                              </button>
                            )}
                          </span>
                        </td>
                      </tr>
                      {resetting === user.id && (
                        <tr className="border-b border-line">
                          <td colSpan={5} className="py-4">
                            <ResetPassword
                              user={user}
                              onDone={() => {
                                setResetting(null);
                              }}
                            />
                          </td>
                        </tr>
                      )}
                    </Fragment>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </section>
  );
}

function tooShort(password: string): string | null {
  return password.length > 0 && password.length < MIN_PASSWORD_LENGTH
    ? `At least ${String(MIN_PASSWORD_LENGTH)} characters.`
    : null;
}

function ResetPassword({ user, onDone }: { user: UserView; onDone: () => void }) {
  const update = useUpdateUser();
  const [password, setPassword] = useState('');
  const hint = tooShort(password);
  const submit = (e: FormEvent) => {
    e.preventDefault();
    update.mutate({ user, change: { password } }, { onSuccess: onDone });
  };
  return (
    <form onSubmit={submit} className="flex flex-wrap items-start gap-3">
      <div className="w-72">
        <label htmlFor={`password-${user.id}`} className="text-[12px] text-muted">
          New password for {user.display_name}
        </label>
        <input
          id={`password-${user.id}`}
          type="password"
          autoComplete="new-password"
          className={`mt-1 ${FIELD}`}
          value={password}
          aria-describedby={hint ? `password-${user.id}-hint` : undefined}
          onChange={(e) => {
            setPassword(e.target.value);
          }}
        />
        {hint && (
          <p id={`password-${user.id}-hint`} className="mt-1 text-[12px] text-warning">
            {hint}
          </p>
        )}
      </div>
      <div className="flex gap-2 pt-6">
        <button
          type="submit"
          className={PRIMARY}
          disabled={update.isPending || password.length < MIN_PASSWORD_LENGTH}
        >
          Set the password
        </button>
        <button type="button" className={SECONDARY} onClick={onDone}>
          Cancel
        </button>
      </div>
      {update.isError && (
        <div className="basis-full">
          <ProblemAlert error={update.error} title="Could not set the password" />
        </div>
      )}
    </form>
  );
}

function AddUser({ onDone }: { onDone: () => void }) {
  const create = useCreateUser();
  const [username, setUsername] = useState('');
  const [name, setName] = useState('');
  const [password, setPassword] = useState('');
  const [role, setRole] = useState<Role>('VIEWER');
  const hint = tooShort(password);
  const ready =
    /^[A-Za-z0-9._-]{3,64}$/.test(username) &&
    name.trim() !== '' &&
    password.length >= MIN_PASSWORD_LENGTH;
  const submit = (e: FormEvent) => {
    e.preventDefault();
    create.mutate({ username, display_name: name.trim(), password, role }, { onSuccess: onDone });
  };
  return (
    <form
      aria-label="Add a user"
      onSubmit={submit}
      className="rounded-md border border-line bg-ground/40 p-5"
    >
      <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-4">
        <div>
          <label htmlFor="new-username" className="text-[12px] text-muted">
            Username
          </label>
          <input
            id="new-username"
            autoComplete="off"
            className={`mt-1 ${FIELD}`}
            value={username}
            onChange={(e) => {
              setUsername(e.target.value);
            }}
          />
          <p className="mt-1 text-[12px] text-muted">3–64 letters, digits, . _ or -</p>
        </div>
        <div>
          <label htmlFor="new-name" className="text-[12px] text-muted">
            Display name
          </label>
          <input
            id="new-name"
            autoComplete="off"
            className={`mt-1 ${FIELD}`}
            value={name}
            onChange={(e) => {
              setName(e.target.value);
            }}
          />
        </div>
        <div>
          <label htmlFor="new-password" className="text-[12px] text-muted">
            Password
          </label>
          <input
            id="new-password"
            type="password"
            autoComplete="new-password"
            className={`mt-1 ${FIELD}`}
            value={password}
            aria-describedby={hint ? 'new-password-hint' : undefined}
            onChange={(e) => {
              setPassword(e.target.value);
            }}
          />
          {hint && (
            <p id="new-password-hint" className="mt-1 text-[12px] text-warning">
              {hint}
            </p>
          )}
        </div>
        <div>
          <label htmlFor="new-role" className="text-[12px] text-muted">
            Role
          </label>
          <select
            id="new-role"
            className={`mt-1 ${FIELD}`}
            value={role}
            onChange={(e) => {
              setRole(e.target.value as Role);
            }}
          >
            {ROLES.map((r) => (
              <option key={r.value} value={r.value}>
                {r.label}
              </option>
            ))}
          </select>
          <p className="mt-1 text-[12px] text-muted">{ROLES.find((r) => r.value === role)?.note}</p>
        </div>
      </div>
      {create.isError && (
        <div className="mt-4">
          <ProblemAlert error={create.error} title="Could not add the user" />
        </div>
      )}
      <div className="mt-5 flex gap-2">
        <button type="submit" className={PRIMARY} disabled={!ready || create.isPending}>
          Add the user
        </button>
        <button type="button" className={SECONDARY} onClick={onDone}>
          Cancel
        </button>
      </div>
    </form>
  );
}
