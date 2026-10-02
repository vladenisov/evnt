import { afterEach, describe, expect, it, vi } from "vitest";
import { initSnowplow, trackPageView, trackTestStructEvent } from "@/lib/snowplow";

type Queue = Window["snowplow"];

function queued(): unknown[][] {
  return (window.snowplow.q ?? []) as unknown[][];
}

afterEach(() => {
  delete (window as Partial<Window>).snowplow;
  delete window.GlobalSnowplowNamespace;
  delete window.evnt;
  document.head.innerHTML = "";
  document.body.innerHTML = "";
});

function trackerOptions(): Record<string, unknown> {
  const call = queued().find((args) => args[0] === "newTracker");
  return call?.[3] as Record<string, unknown>;
}

describe("initSnowplow", () => {
  it("installs the queue, loads sp.js from the collector and configures the tracker", () => {
    initSnowplow({ appId: "app", collectorOrigin: "http://collector.example" });

    expect(window.GlobalSnowplowNamespace).toEqual(["snowplow"]);
    const scripts = [...document.querySelectorAll("script")].map((s) => s.src);
    expect(scripts).toEqual(["http://collector.example/static/sp/sp.js"]);

    const newTracker = queued().find((args) => args[0] === "newTracker");
    expect(newTracker?.slice(0, 3)).toEqual(["newTracker", "sp1", "http://collector.example"]);
    expect(trackerOptions()).toMatchObject({ appId: "app", postPath: "/tracker" });
    expect(trackerOptions()).not.toHaveProperty("customFetch");
    expect(queued().map((args) => args[0])).toEqual([
      "newTracker",
      "enableActivityTracking",
      "enableLinkClickTracking",
      "addGlobalContexts",
    ]);
  });

  it("defaults to the page's origin and sets the user id when given", () => {
    initSnowplow({ userId: "u-1" });
    expect(queued()[0]?.[2]).toBe(window.location.origin);
    expect(queued()).toContainEqual(["setUserId", "u-1"]);
  });

  it("reuses an existing queue", () => {
    const existing = vi.fn() as unknown as Queue;
    window.snowplow = existing;
    initSnowplow();
    expect(existing).toHaveBeenCalledWith("newTracker", "sp1", window.location.origin, expect.any(Object));
    expect(document.querySelectorAll("script")).toHaveLength(0);
  });

  it("inserts sp.js before the first script when there is one", () => {
    const first = document.createElement("script");
    document.body.appendChild(first);
    initSnowplow({ scriptUrl: "http://cdn.example/sp.js" });
    expect(document.body.firstElementChild).not.toBe(first);
    expect((document.body.firstElementChild as HTMLScriptElement).src).toBe("http://cdn.example/sp.js");
  });

  describe("with encryption", () => {
    it("loads the sealer and routes batches through it", async () => {
      initSnowplow({ encrypt: true, collectorOrigin: "http://collector.example" });
      const srcs = [...document.querySelectorAll("script")].map((s) => s.src);
      expect(srcs).toContain("http://collector.example/e.js");

      const opts = trackerOptions();
      expect(opts.dontRetryStatusCodes).toEqual([413]);
      const customFetch = opts.customFetch as (r: Request) => Promise<Response>;
      const request = new Request("http://collector.example/tracker", { method: "POST" });

      // Until the sealer has loaded the batch is rejected, never sent in the clear.
      await expect(customFetch(request)).rejects.toThrow(/sealer not loaded/);

      const response = new Response(null, { status: 204 });
      const encryptedFetch = vi.fn(async () => response);
      window.evnt = {
        encryptedFetch,
        seal: async (p) => p,
        supported: async () => true,
        kid: "k",
        endpoint: "/e",
      };
      await expect(customFetch(request)).resolves.toBe(response);
      expect(encryptedFetch).toHaveBeenCalledWith(request);
    });
  });
});

describe("track helpers", () => {
  it("queue a page view and a struct event", () => {
    initSnowplow();
    trackPageView();
    trackTestStructEvent();
    expect(queued()).toContainEqual(["trackPageView"]);
    expect(queued()).toContainEqual([
      "trackStructEvent",
      "User Actions",
      "Button Click",
      "Track Event Button",
      null,
      null,
    ]);
  });

  it("are no-ops before the tracker is set up", () => {
    expect(() => {
      trackPageView();
      trackTestStructEvent();
    }).not.toThrow();
  });
});
