type SnowplowQ = {
  (...args: unknown[]): void;
  q?: unknown[];
};

function ensureLoader(scriptUrl: string): SnowplowQ {
  if (typeof window === "undefined") {
    throw new Error("snowplow loader requires a browser window");
  }
  if (window.snowplow) return window.snowplow;

  window.GlobalSnowplowNamespace = window.GlobalSnowplowNamespace ?? [];
  window.GlobalSnowplowNamespace.push("snowplow");

  const queue: SnowplowQ = function (...args: unknown[]) {
    (queue.q = queue.q ?? []).push(args);
  };
  queue.q = [];
  window.snowplow = queue;

  const script = document.createElement("script");
  script.async = true;
  script.src = scriptUrl;
  const first = document.getElementsByTagName("script")[0];
  if (first?.parentNode) {
    first.parentNode.insertBefore(script, first);
  } else {
    document.head.appendChild(script);
  }
  return queue;
}

export interface SnowplowInitOptions {
  appId?: string;
  collectorOrigin?: string;
  scriptUrl?: string;
  userId?: string;
  /** Seal payloads with the collector's public key before sending them. */
  encrypt?: boolean;
}

/**
 * Load the sealer the collector serves at `/e.js`.
 *
 * It arrives with the public key and key id already substituted, so nothing
 * about the key lives in this bundle and a rotation needs no frontend deploy.
 */
function loadSealer(collectorOrigin: string): void {
  const script = document.createElement("script");
  script.async = true;
  script.src = new URL("/e.js", collectorOrigin).toString();
  document.head.appendChild(script);
}

/**
 * Hand batches to the sealer, rejecting until it has loaded.
 *
 * Rejecting matters: the tracker catches it and keeps the batch for the next
 * flush, whereas leaving `customFetch` undefined would make it fall back to
 * plain `fetch` and quietly post the payload in the clear.
 */
function encryptedFetch(request: Request): Promise<Response> {
  const seal = window.evnt?.encryptedFetch;
  if (seal) return seal(request);
  return Promise.reject(new Error("evnt: sealer not loaded yet"));
}

export function initSnowplow(options: SnowplowInitOptions = {}): void {
  const collectorOrigin = options.collectorOrigin ?? window.location.origin;
  const scriptUrl =
    options.scriptUrl ?? new URL("/static/sp/sp.js", collectorOrigin).toString();
  if (options.encrypt) loadSealer(collectorOrigin);
  const sp = ensureLoader(scriptUrl);

  sp("newTracker", "sp1", collectorOrigin, {
    appId: options.appId ?? "evnt-demo",
    postPath: "/tracker",
    encodeBase64: false,
    discoverRootDomain: true,
    // The tracker's no-retry list is 400, 401, 403, 410, 422; without this a
    // batch the collector rejects as oversized would be retried forever.
    ...(options.encrypt
      ? { dontRetryStatusCodes: [413], customFetch: encryptedFetch }
      : {}),
    contexts: {
      webPage: true,
      session: true,
      browser: true,
      performanceNavigationTiming: true,
      performanceTiming: true,
      gaCookies: true,
      geolocation: false,
      clientHints: true,
    },
  });

  if (options.userId) {
    sp("setUserId", options.userId);
  }

  sp("enableActivityTracking", { minimumVisitLength: 5, heartbeatDelay: 5 });
  sp("enableLinkClickTracking", { pseudoClicks: true, trackContent: true });

  sp("addGlobalContexts", [
    {
      schema: "iglu:dev.snowplow.simple/page_data",
      data: { id: "qwe123", type: "main", section: null },
    },
    {
      schema: "iglu:dev.snowplow.simple/user_data",
      data: { name: "John", type: "contributor" },
    },
  ]);
}

export function trackPageView(): void {
  window.snowplow?.("trackPageView");
}

export function trackTestStructEvent(): void {
  window.snowplow?.(
    "trackStructEvent",
    "User Actions",
    "Button Click",
    "Track Event Button",
    null,
    null,
  );
}
