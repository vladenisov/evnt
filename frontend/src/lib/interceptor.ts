import { useLiveEvents, type LogMethod } from "@/stores/liveEvents";

/**
 * Collector paths whose traffic the live log shows: the Snowplow POST and GET
 * endpoints (backend defaults; see `common.snowplow.endpoints`). Matched
 * exactly, so `/trackers` or a query to ClickHouse never lands in the log.
 */
export const TRACKER_PATHS: readonly string[] = ["/tracker", "/i"];

function methodOf(value: string | undefined): LogMethod {
  const m = (value ?? "GET").toUpperCase();
  if (m === "GET" || m === "POST") return m;
  return "OTHER";
}

function toUrl(url: string): URL | null {
  try {
    return new URL(url, window.location.origin);
  } catch {
    return null;
  }
}

export function isTrackerUrl(url: string): boolean {
  const parsed = toUrl(url);
  return parsed !== null && TRACKER_PATHS.includes(parsed.pathname);
}

function parseText(text: string): unknown {
  if (!text) return null;
  try {
    return JSON.parse(text);
  } catch {
    return text;
  }
}

/** Turn any request body the tracker may send into something JsonTree can show. */
export async function parseBody(body: unknown): Promise<unknown> {
  if (body === null || body === undefined || body === "") return null;
  if (typeof body === "string") return parseText(body);
  if (body instanceof URLSearchParams) return Object.fromEntries(body.entries());
  if (body instanceof FormData) return Object.fromEntries(body.entries());
  if (body instanceof Blob) return parseText(await body.text());
  if (body instanceof ArrayBuffer || ArrayBuffer.isView(body)) {
    return parseText(new TextDecoder().decode(body));
  }
  return body;
}

export function parseQueryPayload(url: string): unknown {
  const parsed = toUrl(url);
  return parsed ? Object.fromEntries(parsed.searchParams.entries()) : url;
}

let pending: Promise<void> = Promise.resolve();

/**
 * Record one tracker request. The body may need reading asynchronously (a
 * `Request` or `Blob`); the entry keeps the time the request was made, and is
 * dropped if capture was paused at that moment.
 */
function logRequest(method: LogMethod, url: string, body: unknown | Promise<unknown>): void {
  const store = useLiveEvents();
  if (store.paused) return;
  const timestamp = Date.now();
  const payload =
    method === "GET" ? Promise.resolve(parseQueryPayload(url)) : Promise.resolve(body).then(parseBody);
  const settled = payload.catch((error: unknown) => `<unreadable body: ${String(error)}>`);
  // Push in request order: a body that takes longer to read must not let a
  // later request (a GET has nothing to read) jump ahead of it in the log.
  pending = pending
    .then(() => settled)
    .then((value) => store.push({ method, url, timestamp, payload: value }))
    .catch(() => {
      /* the log is best-effort; never break the request it observes */
    });
}

const XHR_META = new WeakMap<XMLHttpRequest, { method: LogMethod; url: string }>();

let uninstall: (() => void) | null = null;

/**
 * Patch XMLHttpRequest, fetch and sendBeacon so every request to the collector
 * shows up in the live log. The request itself always goes through untouched.
 * Returns a function that restores the originals (idempotent install).
 */
export function installInterceptor(): () => void {
  if (uninstall) return uninstall;

  const proto = XMLHttpRequest.prototype;
  const origOpen = proto.open;
  const origSend = proto.send;

  proto.open = function (this: XMLHttpRequest, ...args: Parameters<XMLHttpRequest["open"]>) {
    const [method, url] = args;
    XHR_META.set(this, { method: methodOf(method), url: String(url) });
    return origOpen.apply(this, args);
  } as XMLHttpRequest["open"];

  proto.send = function (this: XMLHttpRequest, body?: Document | XMLHttpRequestBodyInit | null) {
    try {
      const meta = XHR_META.get(this);
      if (meta && isTrackerUrl(meta.url)) logRequest(meta.method, meta.url, body ?? null);
    } catch {
      /* keep the tracker resilient */
    }
    return origSend.call(this, body ?? null);
  };

  const origFetch = typeof window.fetch === "function" ? window.fetch : null;
  if (origFetch) {
    window.fetch = (input: RequestInfo | URL, init?: RequestInit) => {
      try {
        const url =
          typeof input === "string" ? input : input instanceof URL ? input.toString() : input.url;
        if (isTrackerUrl(url)) {
          const method = methodOf(init?.method ?? (input instanceof Request ? input.method : "GET"));
          // The Snowplow tracker (v4) hands fetch a ready-made Request, so the
          // body lives inside it. Clone before the original call consumes it.
          const body =
            init?.body !== undefined && init.body !== null
              ? init.body
              : input instanceof Request
                ? input.clone().text()
                : null;
          logRequest(method, url, body);
        }
      } catch {
        /* ignore */
      }
      return origFetch.call(window, input, init);
    };
  }

  const nav = window.navigator;
  const origSendBeacon = typeof nav.sendBeacon === "function" ? nav.sendBeacon : null;
  if (origSendBeacon) {
    nav.sendBeacon = (url: string | URL, data?: BodyInit | null) => {
      try {
        const u = String(url);
        if (isTrackerUrl(u)) logRequest("POST", u, data ?? null);
      } catch {
        /* ignore */
      }
      return origSendBeacon.call(nav, url, data);
    };
  }

  uninstall = () => {
    proto.open = origOpen;
    proto.send = origSend;
    if (origFetch) window.fetch = origFetch;
    if (origSendBeacon) nav.sendBeacon = origSendBeacon;
    uninstall = null;
  };
  return uninstall;
}
