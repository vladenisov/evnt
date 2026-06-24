import { createClient, type WebClickHouseClient } from "@clickhouse/client-web";
import { useSettings } from "@/stores/settings";

function buildClient(): WebClickHouseClient {
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
  const rs = await client.query({
    query,
    query_params: params,
    format: "JSONEachRow",
  });
  return rs.json<T>();
}

export async function fetchScalar<T = unknown>(
  query: string,
  params?: Record<string, unknown>,
): Promise<T | null> {
  const rows = await fetchRows<Record<string, T>>(query, params);
  if (!rows.length) return null;
  const first = rows[0];
  const keys = Object.keys(first);
  return keys.length ? (first[keys[0]] ?? null) : null;
}

export interface TableInfo {
  database: string;
  name: string;
  engine: string;
  total_rows: number | null;
}

export async function listTables(database: string): Promise<TableInfo[]> {
  return fetchRows<TableInfo>(
    `SELECT database, name, engine, total_rows
       FROM system.tables
       WHERE database = {db:String}
       ORDER BY name`,
    { db: database },
  );
}

export interface ColumnInfo {
  name: string;
  type: string;
}

export async function describeTable(
  database: string,
  table: string,
): Promise<ColumnInfo[]> {
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
  return Number(value ?? 0);
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
  const orderClause = orderBy
    ? `ORDER BY ${escapeIdent(orderBy)} ${desc ? "DESC" : "ASC"}`
    : "";
  return fetchRows<T>(
    `SELECT * FROM ${escapeQualified(qualified)} ${orderClause} LIMIT {l:UInt64} OFFSET {o:UInt64}`,
    { l: limit, o: offset },
  );
}

export function escapeIdent(name: string): string {
  return `\`${name.replace(/`/g, "``")}\``;
}

const QUALIFIED_RE = /^[A-Za-z0-9_]+\.[A-Za-z0-9_]+$/;

export function escapeQualified(qualified: string): string {
  if (!QUALIFIED_RE.test(qualified)) {
    throw new Error(
      `Invalid table identifier: expected "database.table", got "${qualified}"`,
    );
  }
  const [db, tbl] = qualified.split(".");
  return `${escapeIdent(db)}.${escapeIdent(tbl)}`;
}

export async function pingConnection(): Promise<boolean> {
  const value = await fetchScalar<number | string>("SELECT 1");
  return Number(value) === 1;
}
