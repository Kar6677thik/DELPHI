import { zodResolver } from '@hookform/resolvers/zod';
import { useForm } from 'react-hook-form';
import { useNavigate, useSearchParams } from 'react-router';
import { z } from 'zod';

import { useLogin } from '../api/auth';
import { isRateLimited, isUnauthenticated } from '../api/client';
import { DelphiMark } from '../components/DelphiMark';
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
  'mt-2 block h-11 w-full rounded-md border border-control bg-surface px-3 text-[15px] text-ink transition-colors hover:border-muted focus-visible:border-accent focus-visible:outline-none';

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
    <main className="grid min-h-screen bg-ground text-ink lg:grid-cols-[minmax(0,1fr)_480px]">
      <section
        aria-label="About DELPHI"
        className="flex flex-col justify-between border-line px-6 py-8 lg:border-r lg:px-16 lg:py-14"
      >
        <span className="flex items-center gap-2">
          <DelphiMark size={26} />
          <span className="text-[17px] font-semibold tracking-tight">DELPHI</span>
        </span>
        <div className="my-12 max-w-[640px] lg:my-0">
          <p className="text-[40px] font-semibold leading-[1.05] tracking-[-0.03em] lg:text-[64px]">
            Ask before the money moves.
          </p>
          <p className="mt-6 max-w-[520px] text-[16px] leading-relaxed text-soft">
            DELPHI holds a payment for the seconds it takes to ask the payer the one question worth
            asking, then releases, holds or protects it, and explains why.
          </p>
        </div>
        <p className="hidden text-[13px] text-muted lg:block">Demo bank · synthetic data only</p>
      </section>
      <div className="flex items-center justify-center px-6 pb-12 lg:p-12">
        <form
          onSubmit={(event) => {
            void onSubmit(event);
          }}
          noValidate
          aria-labelledby="login-title"
          className="w-full max-w-sm space-y-5"
        >
          <h1 id="login-title" className="text-[26px] font-semibold tracking-tight">
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
            <label htmlFor="username" className="text-[13px] font-medium text-soft">
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
            <label htmlFor="password" className="text-[13px] font-medium text-soft">
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
            className="h-11 w-full rounded-full bg-ink text-[15px] font-semibold text-ground transition-transform duration-100 hover:bg-soft active:scale-[0.99] disabled:opacity-60"
          >
            {login.isPending ? 'Signing in…' : 'Sign in'}
          </button>
        </form>
      </div>
    </main>
  );
}
