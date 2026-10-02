import { createClient, type ClickHouseClient } from "@clickhouse/client-web";
import { useSettings } from "@/stores/settings";

function buildClient(): ClickHouseClient {
  const s = useSettings();
  return createClient({
    url: s.url,
    username: s.user,
    password: s.password,
    database: s.database,
  });
}

export async function fetchRows<T = Record<string, unknown>>(
  query: string,
  params?: Record<string, unknown>,
): Promise<T[]> {
  const client = buildClient();
  try {
    const rs = await client.query({
      query,
      query_params: params,
      format: "JSONEachRow",
    });
    return await rs.json<T>();
  } finally {
    await client.close();
  }
}

export async function fetchScalar<T = unknown>(
  query: string,
  params?: Record<string, unknown>,
): Promise<T | null> {
  const [first] = await fetchRows<Record<string, T>>(query, params);
  if (!first) return null;
  const [key] = Object.keys(first);
  return key === undefined ? null : (first[key] ?? null);
}

/**
 * ClickHouse sends 64-bit integers as JSON strings (they do not fit a JS
 * number), so counts arrive as `"123"`. Normalise them, keeping null as null.
 */
function toCount(value: unknown): number | null {
  if (value === null || value === undefined) return null;
  const n = Number(value);
  return Number.isFinite(n) ? n : null;
}

export interface TableInfo {
  database: string;
  name: string;
  engine: string;
  total_rows: number | null;
}

export async function listTables(database: string): Promise<TableInfo[]> {
  const rows = await fetchRows<Omit<TableInfo, "total_rows"> & { total_rows: unknown }>(
    `SELECT database, name, engine, total_rows
       FROM system.tables
       WHERE database = {db:String}
       ORDER BY name`,
    { db: database },
  );
  return rows.map((row) => ({ ...row, total_rows: toCount(row.total_rows) }));
}

export interface ColumnInfo {
  name: string;
  type: string;
}

export async function describeTable(database: string, table: string): Promise<ColumnInfo[]> {
  return fetchRows<ColumnInfo>(
    `SELECT name, type
       FROM system.columns
       WHERE database = {db:String} AND table = {tbl:String}
       ORDER BY position`,
    { db: database, tbl: table },
  );
}

export async function countRows(qualified: string): Promise<number> {
  const value = await fetchScalar<string | number>(
    `SELECT count() AS c FROM ${escapeQualified(qualified)}`,
  );
  return toCount(value) ?? 0;
}

export interface QueryRowsOptions {
  qualified: string;
  limit: number;
  offset: number;
  orderBy?: string;
  desc?: boolean;
}

export async function queryRows<T = Record<string, unknown>>({
  qualified,
  limit,
  offset,
  orderBy,
  desc,
}: QueryRowsOptions): Promise<T[]> {
  const orderClause = orderBy ? ` ORDER BY ${escapeIdent(orderBy)} ${desc ? "DESC" : "ASC"}` : "";
  return fetchRows<T>(
    `SELECT * FROM ${escapeQualified(qualified)}${orderClause} LIMIT {l:UInt64} OFFSET {o:UInt64}`,
    { l: limit, o: offset },
  );
}

/**
 * Quote an identifier for interpolation into a query.
 *
 * Inside a backtick-quoted identifier ClickHouse treats a backslash as an
 * escape character, so both the backslash and the backtick must be escaped:
 * escaping only the backtick lets a name ending in `\` swallow the closing
 * quote and run the rest of the name as SQL.
 */
export function escapeIdent(name: string): string {
  return `\`${name.replace(/[\\`]/g, (ch) => `\\${ch}`)}\``;
}

const QUALIFIED_RE = /^([A-Za-z0-9_]+)\.([A-Za-z0-9_]+)$/;

export interface QualifiedName {
  database: string;
  table: string;
}

/** Split `database.table`, rejecting anything that is not two plain identifiers. */
export function parseQualified(qualified: string): QualifiedName {
  const match = QUALIFIED_RE.exec(qualified);
  if (!match?.[1] || !match[2]) {
    throw new Error(`Invalid table identifier: expected "database.table", got "${qualified}"`);
  }
  return { database: match[1], table: match[2] };
}

export function escapeQualified(qualified: string): string {
  const { database, table } = parseQualified(qualified);
  return `${escapeIdent(database)}.${escapeIdent(table)}`;
}

export async function pingConnection(): Promise<boolean> {
  const value = await fetchScalar<number | string>("SELECT 1");
  return Number(value) === 1;
}
