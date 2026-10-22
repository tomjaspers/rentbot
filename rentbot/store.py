import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path

from rentbot.models import Listing

SCHEMA = """
create table if not exists listings (
    key text primary key,          -- source:id
    source text not null,
    fingerprint text not null,
    url text not null,
    address text,
    price_pcm integer,
    matched integer not null,      -- 1 if it passed the criteria when last checked
    reason text,                   -- why it was rejected
    message_id integer,            -- Telegram message of the alert, if one was sent
    first_seen real not null,
    last_seen real not null
);
create index if not exists listings_fp on listings(fingerprint);
create table if not exists source_status (
    source text primary key,
    consecutive_failures integer not null default 0,
    last_error text,
    warned integer not null default 0
);
create table if not exists meta (
    key text primary key,
    value text
);
create table if not exists runs (
    id integer primary key autoincrement,
    started real not null,
    finished real,
    ok integer,                    -- 1 when every enabled source succeeded
    details text                   -- JSON: {source: {results, new, alerts, error}}
);
"""

# Columns added after the first release; applied with ALTER TABLE on older databases.
LISTING_COLUMNS = {
    "beds": "integer",
    "property_type": "text",
    "agent": "text",
    "image": "text",
    "garden": "text",
    "garden_reason": "text",
    "status": "text not null default 'new'",  # new | interested | rejected (set from the dashboard)
    "status_changed": "real",
    "data": "text",            # JSON of the full Listing (everything the dashboard shows)
    "criteria_hash": "text",   # config the row was last evaluated against
}

RUN_COLUMNS = {
    "runner": "text",  # which machine ran it: github | laptop
}

STATUSES = ("new", "interested", "rejected")


class Db:
    """Minimal adapter so the same SQL (written with ? placeholders) runs on SQLite (local) and
    Postgres (Neon). Rows come back as dicts."""

    def __init__(self, target: str | Path):
        self.pg = str(target).startswith(("postgres://", "postgresql://"))
        self.url = str(target)
        if self.pg:
            from psycopg.rows import dict_row
            from psycopg_pool import ConnectionPool

            # Neon closes idle connections when compute scales to zero, so check before each use.
            self.pool = ConnectionPool(str(target), min_size=0, max_size=4, open=True,
                                       check=ConnectionPool.check_connection,
                                       kwargs={"row_factory": dict_row, "autocommit": True,
                                               "prepare_threshold": None})  # PgBouncer (pooled URL)
        else:
            self.conn = sqlite3.connect(target, check_same_thread=False)
            self.conn.row_factory = sqlite3.Row
            self._mutex = threading.Lock()  # one SQLite connection shared by dashboard threads

    def close(self) -> None:
        if self.pg:
            self.pool.close()
        else:
            self.conn.close()

    def _sql(self, sql: str) -> str:
        return sql.replace("%", "%%").replace("?", "%s") if self.pg else sql

    def run(self, sql: str, params: tuple = ()) -> tuple[list[dict], int]:
        """Executes one statement; returns (rows, rowcount)."""
        if self.pg:
            with self.pool.connection() as conn:
                cur = conn.execute(self._sql(sql), params)
                rows = cur.fetchall() if cur.description else []
                return rows, cur.rowcount
        with self._mutex:
            cur = self.conn.execute(sql, params)
            rows = [dict(r) for r in cur.fetchall()] if cur.description else []
            self.conn.commit()
            return rows, cur.rowcount

    def all(self, sql: str, params: tuple = ()) -> list[dict]:
        return self.run(sql, params)[0]

    def one(self, sql: str, params: tuple = ()) -> dict | None:
        rows = self.run(sql, params)[0]
        return rows[0] if rows else None

    def ddl(self, schema: str, added_columns: dict[str, dict[str, str]]) -> None:
        if self.pg:
            schema = (schema.replace("integer primary key autoincrement", "bigserial primary key")
                      .replace(" real", " double precision").replace(" integer", " bigint"))
        for stmt in (st.strip() for st in schema.split(";")):
            if stmt:
                self.run(stmt)
        for table, columns in added_columns.items():
            if self.pg:
                for col, ddl in columns.items():
                    ddl = ddl.replace("real", "double precision").replace("integer", "bigint")
                    self.run(f"alter table {table} add column if not exists {col} {ddl}")
            else:
                have = {r["name"] for r in self.all(f"pragma table_info({table})")}
                for col, ddl in columns.items():
                    if col not in have:
                        self.run(f"alter table {table} add column {col} {ddl}")

    @contextmanager
    def lock(self, key: int, timeout: float):
        """Cross-machine mutex (Postgres advisory lock); a no-op on local SQLite. Session-level
        locks don't survive PgBouncer's transaction pooling, so this uses a direct connection."""
        if not self.pg:
            yield
            return
        import psycopg

        direct = self.url.replace("-pooler.", ".", 1)
        with psycopg.connect(direct, autocommit=True) as conn:
            deadline = time.time() + timeout
            while not conn.execute("select pg_try_advisory_lock(%s)", (key,)).fetchone()[0]:
                if time.time() > deadline:
                    raise TimeoutError(f"another run still holds the scan lock after {timeout:.0f}s")
                time.sleep(5)
            try:
                yield
            finally:
                conn.execute("select pg_advisory_unlock(%s)", (key,))


SCAN_LOCK = 4242  # advisory lock id: one scan at a time across GitHub Actions and the laptop


class Store:
    def __init__(self, target: str | Path):
        self.db = Db(target)
        self.db.ddl(SCHEMA, {"listings": LISTING_COLUMNS, "runs": RUN_COLUMNS})

    def run_lock(self, timeout: float = 900):
        return self.db.lock(SCAN_LOCK, timeout)

    def is_empty(self) -> bool:
        return self.db.one("select 1 as x from listings limit 1") is None

    def get(self, key: str) -> dict | None:
        return self.db.one("select * from listings where key = ?", (key,))

    def alerted_with_fingerprint(self, fp: str) -> dict | None:
        """The row whose group alert first announced this house, if any."""
        return self.db.one(
            "select * from listings where fingerprint = ? and message_id is not null order by first_seen limit 1",
            (fp,),
        )

    def rows_for_fingerprint(self, fp: str) -> list[dict]:
        return self.db.all("select * from listings where fingerprint = ? order by first_seen", (fp,))

    def fingerprints_for_message(self, message_id: int) -> int:
        row = self.db.one("select count(distinct fingerprint) as n from listings where message_id = ?", (message_id,))
        return row["n"] if row else 0

    def status_for_fingerprint(self, fp: str) -> str:
        row = self.db.one("select status from listings where fingerprint = ? and status != 'new' limit 1", (fp,))
        return row["status"] if row else "new"

    def get_meta(self, key: str) -> str | None:
        row = self.db.one("select value from meta where key = ?", (key,))
        return row["value"] if row else None

    def set_meta(self, key: str, value: str) -> None:
        self.db.run("insert into meta (key, value) values (?, ?) on conflict(key) do update set value = excluded.value",
                    (key, value))

    def save(self, l: Listing, fp: str, matched: bool, reason: str = "", message_id: int | None = None,
             criteria_hash: str = "") -> None:
        now = time.time()
        self.db.run(
            """insert into listings (key, source, fingerprint, url, address, price_pcm, matched, reason,
                                     message_id, first_seen, last_seen, beds, property_type, agent, image,
                                     garden, garden_reason, status, data, criteria_hash)
               values (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               on conflict(key) do update set fingerprint=excluded.fingerprint, url=excluded.url,
                   address=excluded.address, price_pcm=excluded.price_pcm, matched=excluded.matched,
                   reason=excluded.reason, message_id=coalesce(excluded.message_id, listings.message_id),
                   last_seen=excluded.last_seen, beds=excluded.beds, property_type=excluded.property_type,
                   agent=excluded.agent, image=excluded.image, garden=excluded.garden,
                   garden_reason=excluded.garden_reason, data=excluded.data,
                   criteria_hash=excluded.criteria_hash""",
            (l.key, l.source, fp, l.url, l.address, l.price_pcm, int(matched), reason, message_id, now, now,
             l.beds, l.property_type, l.agent, l.image, l.garden, l.garden_reason,
             self.status_for_fingerprint(fp), json.dumps(l.to_dict()), criteria_hash),
        )

    def touch(self, key: str) -> None:
        self.db.run("update listings set last_seen = ? where key = ?", (time.time(), key))

    def set_status(self, fingerprint: str, status: str) -> int:
        """Applies to every copy of the house (same fingerprint) across sites."""
        if status not in STATUSES:
            raise ValueError(status)
        return self.db.run(
            "update listings set status = ?, status_changed = ? where fingerprint = ?",
            (status, time.time(), fingerprint),
        )[1]

    # --- runs and source health ---

    def start_run(self, runner: str = "") -> int:
        return self.db.one("insert into runs (started, runner) values (?, ?) returning id",
                           (time.time(), runner))["id"]

    def finish_run(self, run_id: int, ok: bool, details: dict) -> None:
        self.db.run(
            "update runs set finished = ?, ok = ?, details = ? where id = ?",
            (time.time(), int(ok), json.dumps(details), run_id),
        )

    def record_success(self, source: str) -> None:
        self.db.run(
            """insert into source_status (source, consecutive_failures, warned) values (?, 0, 0)
               on conflict(source) do update set consecutive_failures = 0, last_error = null, warned = 0""",
            (source,),
        )

    def record_failure(self, source: str, error: str) -> dict:
        self.db.run(
            """insert into source_status (source, consecutive_failures, last_error) values (?, 1, ?)
               on conflict(source) do update set consecutive_failures = source_status.consecutive_failures + 1,
                   last_error = excluded.last_error""",
            (source, error),
        )
        return self.db.one("select * from source_status where source = ?", (source,))

    def get_source_status(self, source: str) -> dict | None:
        return self.db.one("select * from source_status where source = ?", (source,))

    def runs_since(self, ts: float) -> list[dict]:
        return [r | {"details": json.loads(r["details"] or "{}")}
                for r in self.db.all("select * from runs where started >= ? order by id", (ts,))]

    def last_run(self) -> dict | None:
        return self.db.one("select * from runs order by id desc limit 1")

    def mark_warned(self, source: str) -> None:
        self.db.run("update source_status set warned = 1 where source = ?", (source,))

    # --- dashboard ---

    def dashboard_state(self, run_limit: int = 30) -> dict:
        runs = [dict(r) | {"details": json.loads(r["details"] or "{}")}
                for r in self.db.all("select * from runs order by id desc limit ?", (run_limit,))]
        sources = self.db.all("select * from source_status order by source")
        rows = self.db.all("select * from listings where matched = 1 order by first_seen desc")
        groups: dict[str, dict] = {}
        for r in rows:  # one card per house, with a link per site it appears on
            g = groups.get(r["fingerprint"])
            if g is None:
                data = json.loads(r["data"] or "{}")
                data.pop("description", None)  # long; not shown on the dashboard
                g = groups[r["fingerprint"]] = data | dict(r) | {"links": []}
                g.pop("data", None)
            g["links"].append({"source": r["source"], "url": r["url"]})
            g["first_seen"] = min(g["first_seen"], r["first_seen"])
            g["last_seen"] = max(g["last_seen"], r["last_seen"])
            g["image"] = g["image"] or r["image"]
            if r["status"] != "new":
                g["status"] = r["status"]
        return {"runs": runs, "sources": sources, "listings": list(groups.values())}
