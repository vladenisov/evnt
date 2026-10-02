import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { createPinia, setActivePinia } from "pinia";
import { installInterceptor, isTrackerUrl, parseBody, parseQueryPayload } from "@/lib/interceptor";
import { useLiveEvents } from "@/stores/liveEvents";

describe("isTrackerUrl", () => {
  it.each([
    ["/tracker", true],
    ["http://collector.example/tracker", true],
    ["/i?e=pv&aid=x", true],
    ["/trackers", false],
    ["/static/sp/sp.js", false],
    ["/demo/live", false],
    ["http://localhost:8123/?query=SELECT%201", false],
    ["http://[bad", false],
  ])("%s -> %s", (url, expected) => {
    expect(isTrackerUrl(url)).toBe(expected);
  });
});

describe("parseBody", () => {
  it("parses JSON strings and keeps other text", async () => {
    await expect(parseBody('{"a":1}')).resolves.toEqual({ a: 1 });
    await expect(parseBody("plain")).resolves.toBe("plain");
  });

  it("returns null for empty bodies", async () => {
    await expect(parseBody(null)).resolves.toBeNull();
    await expect(parseBody(undefined)).resolves.toBeNull();
    await expect(parseBody("")).resolves.toBeNull();
  });

  it("reads Blobs and byte buffers", async () => {
    await expect(parseBody(new Blob(['{"b":2}']))).resolves.toEqual({ b: 2 });
    const bytes = new TextEncoder().encode('{"c":3}');
    await expect(parseBody(bytes)).resolves.toEqual({ c: 3 });
    await expect(parseBody(bytes.buffer)).resolves.toEqual({ c: 3 });
  });

  it("flattens form-like bodies", async () => {
    await expect(parseBody(new URLSearchParams("a=1&b=2"))).resolves.toEqual({ a: "1", b: "2" });
    const form = new FormData();
    form.set("x", "y");
    await expect(parseBody(form)).resolves.toEqual({ x: "y" });
  });

  it("passes anything else through", async () => {
    const obj = { k: 1 };
    await expect(parseBody(obj)).resolves.toBe(obj);
  });
});

describe("parseQueryPayload", () => {
  it("turns the query string into an object", () => {
    expect(parseQueryPayload("/i?e=pv&url=http%3A%2F%2Fx")).toEqual({ e: "pv", url: "http://x" });
  });

  it("returns the raw string when the URL cannot be parsed", () => {
    expect(parseQueryPayload("http://[bad")).toBe("http://[bad");
  });
});

describe("installInterceptor", () => {
  let restore: (() => void) | null = null;
  const realFetch = vi.fn(async () => new Response(null, { status: 204 }));
  const realBeacon = vi.fn(() => true);
  const realOpen = vi.fn();
  const realSend = vi.fn();
  let savedXhr: { open: XMLHttpRequest["open"]; send: XMLHttpRequest["send"] };

  async function settle() {
    for (let i = 0; i < 5; i++) await new Promise((r) => setTimeout(r, 0));
  }

  beforeEach(() => {
    setActivePinia(createPinia());
    realFetch.mockClear();
    realBeacon.mockClear();
    realOpen.mockClear();
    realSend.mockClear();
    savedXhr = { open: XMLHttpRequest.prototype.open, send: XMLHttpRequest.prototype.send };
    XMLHttpRequest.prototype.open = realOpen as unknown as XMLHttpRequest["open"];
    XMLHttpRequest.prototype.send = realSend as unknown as XMLHttpRequest["send"];
    vi.stubGlobal("fetch", realFetch);
    Object.defineProperty(window.navigator, "sendBeacon", {
      value: realBeacon,
      configurable: true,
      writable: true,
    });
    restore = installInterceptor();
  });

  afterEach(() => {
    restore?.();
    XMLHttpRequest.prototype.open = savedXhr.open;
    XMLHttpRequest.prototype.send = savedXhr.send;
  });

  it("is idempotent and fully reversible", () => {
    expect(installInterceptor()).toBe(restore);
    expect(window.fetch).not.toBe(realFetch);
    restore?.();
    restore = null;
    expect(window.fetch).toBe(realFetch);
    expect(window.navigator.sendBeacon).toBe(realBeacon);
    expect(XMLHttpRequest.prototype.open).toBe(realOpen);
  });

  it("logs the body of a fetch made with a Request, as the v4 tracker does", async () => {
    const request = new Request("http://localhost/tracker", {
      method: "POST",
      body: JSON.stringify({ schema: "payload_data", data: [{ e: "pv" }] }),
    });
    await window.fetch(request);
    await settle();

    // The original still receives an unread body.
    const passed = realFetch.mock.calls[0] as unknown as [Request];
    expect(passed[0].bodyUsed).toBe(false);
    const [log] = useLiveEvents().logs;
    expect(log).toMatchObject({
      method: "POST",
      url: "http://localhost/tracker",
      payload: { schema: "payload_data", data: [{ e: "pv" }] },
    });
  });

  it("logs fetch init bodies and GET query strings", async () => {
    await window.fetch("/tracker", { method: "post", body: '{"a":1}' });
    await window.fetch(new URL("http://localhost/i?e=se&se_ca=x"));
    await settle();
    const logs = useLiveEvents().logs;
    expect(logs.map((l) => l.method)).toEqual(["GET", "POST"]);
    expect(logs[0]?.payload).toEqual({ e: "se", se_ca: "x" });
    expect(logs[1]?.payload).toEqual({ a: 1 });
  });

  it("ignores requests that are not for the collector", async () => {
    await window.fetch("http://localhost:8123/?query=SELECT%201", { method: "POST", body: "x" });
    await settle();
    expect(useLiveEvents().logs).toHaveLength(0);
    expect(realFetch).toHaveBeenCalledOnce();
  });

  it("does not log while paused, but still sends", async () => {
    useLiveEvents().togglePaused();
    await window.fetch("/tracker", { method: "POST", body: "{}" });
    await settle();
    expect(useLiveEvents().logs).toHaveLength(0);
    expect(realFetch).toHaveBeenCalledOnce();
  });

  it("logs sendBeacon payloads, Blobs included", async () => {
    const ok = window.navigator.sendBeacon("/tracker", new Blob(['{"beacon":true}']));
    await settle();
    expect(ok).toBe(true);
    expect(realBeacon).toHaveBeenCalledOnce();
    expect(useLiveEvents().logs[0]?.payload).toEqual({ beacon: true });
  });

  it("logs XHR requests and forwards the original arguments", async () => {
    const xhr = new XMLHttpRequest();
    xhr.open("POST", "/tracker", true);
    xhr.send('{"x":1}');
    const other = new XMLHttpRequest();
    other.open("GET", "/elsewhere");
    other.send();
    await settle();
    expect(realOpen).toHaveBeenCalledWith("POST", "/tracker", true);
    expect(realSend).toHaveBeenCalledTimes(2);
    const logs = useLiveEvents().logs;
    expect(logs).toHaveLength(1);
    expect(logs[0]).toMatchObject({ method: "POST", url: "/tracker", payload: { x: 1 } });
  });

  it("records an unreadable body instead of dropping the entry", async () => {
    const request = new Request("http://localhost/tracker", { method: "POST", body: "{}" });
    vi.spyOn(request, "clone").mockReturnValue({
      text: () => Promise.reject(new Error("boom")),
    } as unknown as Request);
    await window.fetch(request);
    await settle();
    expect(useLiveEvents().logs[0]?.payload).toMatch(/unreadable body: Error: boom/);
  });
});
