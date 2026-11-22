import type { ReactNode } from 'react';

/**
 * The customer's phone, drawn: a 390×844 handset at the demo's scale, with the status bar, the
 * camera island and the side keys, so the projector shows a phone rather than a box. `sms` drops
 * the mock SMS in from the top, as the OTP probe's code arrives (docs/08 §4).
 */
export function PhoneFrame({
  children,
  sms,
  clock,
  app = 'DemoBank',
}: {
  children: ReactNode;
  sms?: string | null;
  /** The status-bar time (HH:MM), so screenshots and tests are stable. */
  clock: string;
  app?: string;
}) {
  return (
    <div className="relative mx-auto w-full max-w-[372px] shrink-0">
      {/* side keys */}
      <span
        aria-hidden="true"
        className="absolute -left-[3px] top-[120px] h-8 w-[3px] rounded-l bg-[#2a2a2e]"
      />
      <span
        aria-hidden="true"
        className="absolute -left-[3px] top-[176px] h-14 w-[3px] rounded-l bg-[#2a2a2e]"
      />
      <span
        aria-hidden="true"
        className="absolute -left-[3px] top-[240px] h-14 w-[3px] rounded-l bg-[#2a2a2e]"
      />
      <span
        aria-hidden="true"
        className="absolute -right-[3px] top-[200px] h-20 w-[3px] rounded-r bg-[#2a2a2e]"
      />
      <div className="rounded-[54px] border border-[#2a2a2e] bg-[#0a0a0b] p-[10px]">
        <div
          role="region"
          aria-label="Customer’s phone"
          className="relative flex h-[740px] flex-col overflow-hidden rounded-[44px] bg-[#0e0e10] text-white"
        >
          <div className="relative flex h-12 shrink-0 items-center justify-between px-7 text-[13px] font-semibold">
            <span className="figure">{clock}</span>
            <span
              aria-hidden="true"
              className="absolute left-1/2 top-2.5 h-[30px] w-[108px] -translate-x-1/2 rounded-full bg-black"
            />
            <span aria-hidden="true" className="flex items-center gap-1.5">
              <svg viewBox="0 0 18 12" className="h-3 w-[18px]">
                <rect x="0" y="8" width="3" height="4" rx="1" fill="currentColor" />
                <rect x="5" y="5" width="3" height="7" rx="1" fill="currentColor" />
                <rect x="10" y="2.5" width="3" height="9.5" rx="1" fill="currentColor" />
                <rect x="15" y="0" width="3" height="12" rx="1" fill="currentColor" />
              </svg>
              <svg viewBox="0 0 26 12" className="h-3 w-[26px]">
                <rect
                  x="0.5"
                  y="0.5"
                  width="22"
                  height="11"
                  rx="3"
                  fill="none"
                  stroke="currentColor"
                  opacity=".5"
                />
                <rect x="2" y="2" width="16" height="8" rx="1.5" fill="currentColor" />
                <rect
                  x="23.5"
                  y="4"
                  width="1.5"
                  height="4"
                  rx=".75"
                  fill="currentColor"
                  opacity=".5"
                />
              </svg>
            </span>
          </div>
          {sms && (
            <div
              role="status"
              className="absolute inset-x-3 top-12 z-10 animate-[sms-in_.35s_ease-out] rounded-2xl bg-[#2a2a2e] px-4 py-3"
            >
              <p className="text-[12px] text-white/60">Messages · now</p>
              <p className="mt-0.5 text-[14px] leading-snug">
                <span className="font-semibold">{app}:</span> {sms} is your one-time code for this
                payment. Never share it with anyone, not even a bank officer.
              </p>
            </div>
          )}
          <div className="flex h-11 shrink-0 items-center px-6 text-[15px] font-semibold tracking-tight">
            {app}
          </div>
          <div className="flex min-h-0 flex-1 flex-col overflow-y-auto px-6 pb-6">{children}</div>
          <span
            aria-hidden="true"
            className="mx-auto mb-2 h-[5px] w-[134px] shrink-0 rounded-full bg-white/80"
          />
        </div>
      </div>
    </div>
  );
}
