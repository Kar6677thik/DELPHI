/** jsdom has no EventSource. Tests drive this one by hand. */
export class FakeEventSource {
  static readonly CONNECTING = 0;
  static readonly OPEN = 1;
  static readonly CLOSED = 2;
  static instances: FakeEventSource[] = [];

  readonly url: string;
  readyState = FakeEventSource.CONNECTING;
  onopen: (() => void) | null = null;
  onerror: (() => void) | null = null;
  closed = false;
  readonly listeners = new Map<string, ((event: MessageEvent) => void)[]>();

  constructor(url: string) {
    this.url = url;
    FakeEventSource.instances.push(this);
  }

  addEventListener(type: string, listener: (event: MessageEvent) => void): void {
    this.listeners.set(type, [...(this.listeners.get(type) ?? []), listener]);
  }

  /** A named frame (`id: <id>`, `event: <type>`), as the server sends them (docs/16 §3). */
  emit(type: string, id: string): void {
    const event = new MessageEvent(type, { data: '{}', lastEventId: id });
    for (const listener of this.listeners.get(type) ?? []) listener(event);
  }

  open(): void {
    this.readyState = FakeEventSource.OPEN;
    this.onopen?.();
  }

  /** `retrying`: a dropped connection the browser retries itself; otherwise it gives up. */
  fail({ retrying }: { retrying: boolean }): void {
    this.readyState = retrying ? FakeEventSource.CONNECTING : FakeEventSource.CLOSED;
    this.onerror?.();
  }

  close(): void {
    this.closed = true;
    this.readyState = FakeEventSource.CLOSED;
  }

  static latest(): FakeEventSource {
    const last = FakeEventSource.instances.at(-1);
    if (!last) throw new Error('no EventSource was created');
    return last;
  }
}
