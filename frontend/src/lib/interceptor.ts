import { useLiveEvents, type LogMethod } from "@/stores/liveEvents";

const TRACKER_PATH = "/tracker";

function methodOf(value: string | undefined): LogMethod {
  const m = (value ?? "GET").toUpperCase();
  if (m === "GET" || m === "POST") return m;
  return "OTHER";
}

function isTrackerUrl(url: string): boolean {
  try {
    const parsed = new URL(url, window.location.origin);
    return parsed.pathname.includes(TRACKER_PATH);
  } catch {
    return url.includes(TRACKER_PATH);
  }
}

function parsePostBody(body: unknown): unknown {
  if (!body) return null;
  if (typeof body === "string") {
    try {
      return JSON.parse(body);
    } catch {
      return body;
    }
  }
  if (body instanceof URLSearchParams) {
    return Object.fromEntries(body.entries());
  }
  if (body instanceof FormData) {
    return Object.fromEntries(Array.from(body.entries()));
  }
  return body;
}

function parseGetPayload(url: string): unknown {
  try {
    const parsed = new URL(url, window.location.origin);
    return Object.fromEntries(parsed.searchParams.entries());
  } catch {
    return url;
  }
}

function logRequest(method: LogMethod, url: string, body: unknown) {
  if (!isTrackerUrl(url)) return;
  const store = useLiveEvents();
  const payload = method === "GET" ? parseGetPayload(url) : parsePostBody(body);
  store.push({
    method,
    url,
    timestamp: Date.now(),
    payload,
  });
}

interface XhrMeta {
  _evntMethod?: LogMethod;
  _evntUrl?: string;
}

let installed = false;

export function installInterceptor(): void {
  if (installed) return;
  installed = true;

  const origOpen = XMLHttpRequest.prototype.open;
  const origSend = XMLHttpRequest.prototype.send;

  XMLHttpRequest.prototype.open = function (
    this: XMLHttpRequest & XhrMeta,
    method: string,
    url: string | URL,
    ...rest: unknown[]
  ) {
    this._evntMethod = methodOf(method);
    this._evntUrl = typeof url === "string" ? url : url.toString();
    // @ts-expect-error rest spread to original signature
    return origOpen.call(this, method, url, ...rest);
  };

  XMLHttpRequest.prototype.send = function (
    this: XMLHttpRequest & XhrMeta,
    body?: Document | XMLHttpRequestBodyInit | null,
  ) {
    try {
      const method = this._evntMethod ?? "GET";
      const url = this._evntUrl ?? "";
      if (isTrackerUrl(url)) {
        logRequest(method, url, body ?? null);
      }
    } catch {
      /* keep tracker resilient */
    }
    return origSend.call(this, body ?? null);
  };

  const origFetch = window.fetch?.bind(window);
  if (origFetch) {
    window.fetch = (input: RequestInfo | URL, init?: RequestInit) => {
      try {
        const url =
          typeof input === "string"
            ? input
            : input instanceof URL
              ? input.toString()
              : input.url;
        const method = methodOf(
          init?.method ?? (input instanceof Request ? input.method : "GET"),
        );
        const body =
          init?.body ?? (input instanceof Request ? undefined : undefined);
        if (isTrackerUrl(url)) {
          logRequest(method, url, body ?? null);
        }
      } catch {
        /* ignore */
      }
      return origFetch(input, init);
    };
  }

  const origSendBeacon = navigator.sendBeacon?.bind(navigator);
  if (origSendBeacon) {
    navigator.sendBeacon = (url: string | URL, data?: BodyInit | null) => {
      try {
        const u = typeof url === "string" ? url : url.toString();
        if (isTrackerUrl(u)) {
          logRequest("POST", u, data ?? null);
        }
      } catch {
        /* ignore */
      }
      return origSendBeacon(url, data);
    };
  }
}
