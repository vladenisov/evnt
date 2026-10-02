import { beforeEach, describe, expect, it, vi } from "vitest";
import { createPinia, setActivePinia } from "pinia";

const query = vi.fn();
const close = vi.fn(async () => {});
const createClient = vi.fn((_config: unknown) => ({ query, close }));

vi.mock("@clickhouse/client-web", () => ({
  createClient: (config: unknown) => createClient(config),
}));

const ch = await import("@/lib/clickhouse");
const { useSettings } = await import("@/stores/settings");

function respond(rows: unknown[]) {
  query.mockResolvedValueOnce({ json: async () => rows });
}

beforeEach(() => {
  setActivePinia(createPinia());
  query.mockReset();
  close.mockClear();
  createClient.mockClear();
});

describe("escapeIdent", () => {
  it("wraps a plain name in backticks", () => {
    expect(ch.escapeIdent("time")).toBe("`time`");
  });

  it("escapes backticks", () => {
    expect(ch.escapeIdent("a`b")).toBe("`a\\`b`");
  });

  it("escapes backslashes so a trailing one cannot eat the closing quote", () => {
    // Before the fix this produced `a\``, 1) --` : the closing quote was
    // escaped and everything after it ran as SQL.
    expect(ch.escapeIdent("a\\`, 1) --")).toBe("`a\\\\\\`, 1) --`");
    expect(ch.escapeIdent("x\\")).toBe("`x\\\\`");
  });
});

describe("parseQualified / escapeQualified", () => {
  it("splits database.table", () => {
    expect(ch.parseQualified("evnt.local")).toEqual({ database: "evnt", table: "local" });
    expect(ch.escapeQualified("evnt.local")).toBe("`evnt`.`local`");
  });

  it.each(["evnt", "evnt.", ".local", "a.b.c", "evnt.lo`cal", "evnt.local; DROP", ""])(
    "rejects %j",
    (value) => {
      expect(() => ch.parseQualified(value)).toThrow(/Invalid table identifier/);
    },
  );
});

describe("queries", () => {
  it("builds the client from the current settings and closes it", async () => {
    const settings = useSettings();
    settings.url = "http://ch:8123";
    settings.user = "u";
    settings.password = "p";
    settings.database = "db";
    respond([{ x: 1 }]);

    await expect(ch.fetchRows("SELECT 1 AS x")).resolves.toEqual([{ x: 1 }]);
    expect(createClient).toHaveBeenCalledWith({
      url: "http://ch:8123",
      username: "u",
      password: "p",
      database: "db",
    });
    expect(query).toHaveBeenCalledWith({
      query: "SELECT 1 AS x",
      query_params: undefined,
      format: "JSONEachRow",
    });
    expect(close).toHaveBeenCalledOnce();
  });

  it("propagates query errors and still closes the client", async () => {
    query.mockRejectedValueOnce(new Error("Code: 60. Table does not exist"));
    await expect(ch.fetchRows("SELECT * FROM nope")).rejects.toThrow("Table does not exist");
    expect(close).toHaveBeenCalledOnce();
  });

  it("fetchScalar returns the first column of the first row, or null", async () => {
    respond([{ c: 7, other: 1 }]);
    await expect(ch.fetchScalar("q")).resolves.toBe(7);
    respond([]);
    await expect(ch.fetchScalar("q")).resolves.toBeNull();
    respond([{}]);
    await expect(ch.fetchScalar("q")).resolves.toBeNull();
    respond([{ c: null }]);
    await expect(ch.fetchScalar("q")).resolves.toBeNull();
  });

  it("countRows quotes the table and converts the 64-bit string count", async () => {
    respond([{ c: "12345" }]);
    await expect(ch.countRows("evnt.local")).resolves.toBe(12345);
    expect(query.mock.calls[0]?.[0].query).toBe("SELECT count() AS c FROM `evnt`.`local`");
    respond([]);
    await expect(ch.countRows("evnt.local")).resolves.toBe(0);
  });

  it("countRows refuses an invalid identifier before querying", async () => {
    await expect(ch.countRows("evnt.local x")).rejects.toThrow(/Invalid table identifier/);
    expect(query).not.toHaveBeenCalled();
  });

  it("listTables binds the database and normalises total_rows", async () => {
    respond([
      { database: "evnt", name: "local", engine: "MergeTree", total_rows: "42" },
      { database: "evnt", name: "view", engine: "View", total_rows: null },
    ]);
    await expect(ch.listTables("evnt")).resolves.toEqual([
      { database: "evnt", name: "local", engine: "MergeTree", total_rows: 42 },
      { database: "evnt", name: "view", engine: "View", total_rows: null },
    ]);
    expect(query.mock.calls[0]?.[0].query_params).toEqual({ db: "evnt" });
  });

  it("describeTable binds both names as parameters", async () => {
    respond([{ name: "time", type: "DateTime" }]);
    await expect(ch.describeTable("evnt", "local")).resolves.toEqual([
      { name: "time", type: "DateTime" },
    ]);
    expect(query.mock.calls[0]?.[0].query_params).toEqual({ db: "evnt", tbl: "local" });
  });

  it("queryRows escapes the order column and binds limit/offset", async () => {
    respond([]);
    await ch.queryRows({ qualified: "evnt.local", limit: 25, offset: 50, orderBy: "ti`me", desc: true });
    const call = query.mock.calls[0]?.[0];
    expect(call.query).toBe(
      "SELECT * FROM `evnt`.`local` ORDER BY `ti\\`me` DESC LIMIT {l:UInt64} OFFSET {o:UInt64}",
    );
    expect(call.query_params).toEqual({ l: 25, o: 50 });

    respond([]);
    await ch.queryRows({ qualified: "evnt.local", limit: 10, offset: 0, orderBy: "time" });
    expect(query.mock.calls[1]?.[0].query).toContain("ORDER BY `time` ASC");

    respond([]);
    await ch.queryRows({ qualified: "evnt.local", limit: 10, offset: 0 });
    expect(query.mock.calls[2]?.[0].query).toBe(
      "SELECT * FROM `evnt`.`local` LIMIT {l:UInt64} OFFSET {o:UInt64}",
    );
  });

  it("pingConnection is true only for a 1", async () => {
    respond([{ "1": "1" }]);
    await expect(ch.pingConnection()).resolves.toBe(true);
    respond([{ "1": 0 }]);
    await expect(ch.pingConnection()).resolves.toBe(false);
  });
});
