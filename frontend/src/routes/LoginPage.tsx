import { zodResolver } from '@hookform/resolvers/zod';
import { useForm } from 'react-hook-form';
import { useNavigate, useSearchParams } from 'react-router';
import { z } from 'zod';

import { useLogin } from '../api/auth';
import { isRateLimited, isUnauthenticated } from '../api/client';
import { ProblemAlert } from '../components/ProblemAlert';
import { safeNext } from '../lib/safeNext';

const credentials = z.object({
  username: z.string().trim().min(1, 'Enter your username.'),
  password: z.string().min(1, 'Enter your password.'),
});
type Credentials = z.infer<typeof credentials>;

/** The lockout (docs/16 §1: 5 failures in 15 min), with when to try again if the api says. */
export function lockoutMessage(retryAfterSeconds: number | null): string {
  if (retryAfterSeconds === null) return 'Too many failed sign-in attempts. Try again later.';
  const minutes = Math.max(1, Math.ceil(retryAfterSeconds / 60));
  return `Too many failed sign-in attempts. Try again in ${String(minutes)} minute${minutes === 1 ? '' : 's'}.`;
}

const inputClass =
  'mt-1 block w-full rounded-md border border-slate-300 px-3 py-2 focus-visible:outline-2 focus-visible:outline-slate-900';

export function LoginPage() {
  const login = useLogin();
  const navigate = useNavigate();
  const [params] = useSearchParams();
  const {
    register,
    handleSubmit,
    formState: { errors },
  } = useForm<Credentials>({ resolver: zodResolver(credentials) });

  const onSubmit = handleSubmit((values) => {
    login.mutate(values, {
      onSuccess: () => {
        void navigate(safeNext(params.get('next')), { replace: true });
      },
    });
  });

  return (
    <main className="flex min-h-screen items-center justify-center bg-slate-50 p-4 text-slate-900">
      <form
        onSubmit={(event) => {
          void onSubmit(event);
        }}
        noValidate
        aria-labelledby="login-title"
        className="w-full max-w-sm space-y-4 rounded-lg border border-slate-200 bg-white p-6 shadow-sm"
      >
        <h1 id="login-title" className="text-xl font-semibold">
          Sign in to DELPHI
        </h1>
        {login.isError &&
          (isUnauthenticated(login.error) ? (
            // Generic on purpose: never say which of the two was wrong (docs/19 §1).
            <p role="alert" className="text-sm text-red-800">
              Invalid username or password.
            </p>
          ) : isRateLimited(login.error) ? (
            <p role="alert" className="text-sm text-red-800">
              {lockoutMessage(login.error.retryAfterSeconds)}
            </p>
          ) : (
            <ProblemAlert error={login.error} title="Sign-in failed" />
          ))}
        <div>
          <label htmlFor="username" className="text-sm font-medium">
            Username
          </label>
          <input
            id="username"
            autoComplete="username"
            aria-invalid={errors.username ? true : undefined}
            aria-describedby={errors.username ? 'username-error' : undefined}
            className={inputClass}
            {...register('username')}
          />
          {errors.username && (
            <p id="username-error" className="mt-1 text-sm text-red-800">
              {errors.username.message}
            </p>
          )}
        </div>
        <div>
          <label htmlFor="password" className="text-sm font-medium">
            Password
          </label>
          <input
            id="password"
            type="password"
            autoComplete="current-password"
            aria-invalid={errors.password ? true : undefined}
            aria-describedby={errors.password ? 'password-error' : undefined}
            className={inputClass}
            {...register('password')}
          />
          {errors.password && (
            <p id="password-error" className="mt-1 text-sm text-red-800">
              {errors.password.message}
            </p>
          )}
        </div>
        <button
          type="submit"
          disabled={login.isPending}
          className="w-full rounded-md bg-slate-900 px-3 py-2 font-medium text-white hover:bg-slate-800 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-slate-900 disabled:opacity-60"
        >
          {login.isPending ? 'Signing in…' : 'Sign in'}
        </button>
      </form>
    </main>
  );
}
