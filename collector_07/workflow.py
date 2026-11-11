"""规则采集 --run 工作流：版本化归档、历史选取与变更通知。

设计约束：
- 规则身份为 rule+version；正文/范围/生效日相同而载体(format)变化不产生新版本，
  只在 carriers 中追加载体记录。
- 所有业务表只追加、不删除：来源暂时不可用(503)或缓存未知(304)都不能抹掉历史依据。
- published_at 是规则“可知时间”，effective_at 是业务生效时间；历史选取同时受
  effective_on 与 known_on 约束，known_on 之前尚未发布的规则即使追溯生效也不可选。
"""

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .adapter import decode
from .sources import FixtureTransport

RESULT_CAPTURED = "captured"
RESULT_UNCHANGED = "unchanged"
RESULT_UNKNOWN_CACHE = "unknown_cache"
RESULT_UNAVAILABLE = "unavailable"
RESULT_ERROR = "error"

REASON_RETROACTIVE = "retroactive_effective"


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def parse_day(value, field):
    if not isinstance(value, str):
        raise ValueError(f"{field} 必须是 YYYY-MM-DD 字符串")
    try:
        datetime.strptime(value, "%Y-%m-%d")
    except ValueError as error:
        raise ValueError(f"{field} 必须是 YYYY-MM-DD 日期") from error
    return value


class PolicyStore:
    """规则版本状态库。全部 schema 只追加，任何采集结果都不触发 DELETE。"""

    def __init__(self, path):
        self.path = Path(path)

    def connect(self):
        connection = sqlite3.connect(str(self.path))
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def initialize(self):
        with self.connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS rule_versions (
                    rule TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    published_at TEXT NOT NULL,
                    effective_at TEXT NOT NULL,
                    text TEXT NOT NULL,
                    scope TEXT NOT NULL,
                    first_run TEXT NOT NULL,
                    seen_at TEXT NOT NULL,
                    PRIMARY KEY (rule, version)
                );
                CREATE TABLE IF NOT EXISTS carriers (
                    id INTEGER PRIMARY KEY,
                    rule TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    format TEXT NOT NULL,
                    route TEXT NOT NULL,
                    etag TEXT,
                    first_run TEXT NOT NULL,
                    seen_at TEXT NOT NULL,
                    UNIQUE (rule, version, format, route)
                );
                CREATE TABLE IF NOT EXISTS source_cache (
                    route TEXT PRIMARY KEY,
                    etag TEXT,
                    rule TEXT,
                    version INTEGER,
                    updated_run TEXT NOT NULL,
                    seen_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS observations (
                    id INTEGER PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    route TEXT NOT NULL,
                    status INTEGER NOT NULL,
                    result TEXT NOT NULL,
                    etag TEXT,
                    seen_at TEXT NOT NULL,
                    UNIQUE (run_id, route)
                );
                CREATE TABLE IF NOT EXISTS products (
                    sku TEXT PRIMARY KEY,
                    category TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS audits (
                    id TEXT PRIMARY KEY,
                    sku TEXT NOT NULL,
                    on_date TEXT NOT NULL,
                    rule TEXT NOT NULL,
                    rule_version INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS notifications (
                    id INTEGER PRIMARY KEY,
                    sku TEXT NOT NULL,
                    rule TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    reason TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    created_run TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE (sku, rule, version)
                );
                """
            )

    # -- 上下文（商品范围、既有审核） -------------------------------------------------

    def upsert_context(self, context):
        with self.connect() as connection:
            for product in context.get("products", []):
                connection.execute(
                    "INSERT OR IGNORE INTO products(sku, category) VALUES(?, ?)",
                    (product["sku"], product["category"]),
                )
            for audit in context.get("audits", []):
                connection.execute(
                    "INSERT OR IGNORE INTO audits(id, sku, on_date, rule, rule_version)"
                    " VALUES(?, ?, ?, ?, ?)",
                    (audit["id"], audit["sku"], audit["on"], audit["rule"], audit["rule_version"]),
                )

    def list_products(self):
        with self.connect() as connection:
            return [dict(row) for row in connection.execute("SELECT sku, category FROM products ORDER BY sku")]

    def list_audits(self):
        with self.connect() as connection:
            rows = connection.execute("SELECT id, sku, on_date, rule, rule_version FROM audits ORDER BY id")
            return [
                {"id": row["id"], "sku": row["sku"], "on": row["on_date"], "rule": row["rule"], "rule_version": row["rule_version"]}
                for row in rows
            ]

    # -- 规则版本与载体 ---------------------------------------------------------------

    def ingest_entry(self, entry, route, etag, run_id):
        rule = entry["rule"]
        version = entry["version"]
        now = utc_now()
        with self.connect() as connection:
            connection.execute(
                "INSERT OR IGNORE INTO rule_versions"
                "(rule, version, published_at, effective_at, text, scope, first_run, seen_at)"
                " VALUES(?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    rule,
                    version,
                    entry["published_at"],
                    entry["effective_at"],
                    entry["text"],
                    json.dumps(entry["scope"], ensure_ascii=False),
                    run_id,
                    now,
                ),
            )
            connection.execute(
                "INSERT OR IGNORE INTO carriers"
                "(rule, version, format, route, etag, first_run, seen_at)"
                " VALUES(?, ?, ?, ?, ?, ?, ?)",
                (rule, version, entry["format"], route, etag, run_id, now),
            )
            connection.execute(
                "INSERT INTO source_cache(route, etag, rule, version, updated_run, seen_at)"
                " VALUES(?, ?, ?, ?, ?, ?)"
                " ON CONFLICT(route) DO UPDATE SET"
                " etag=excluded.etag, rule=excluded.rule, version=excluded.version,"
                " updated_run=excluded.updated_run, seen_at=excluded.seen_at",
                (route, etag, rule, version, run_id, now),
            )

    def cached_rule(self, route, etag):
        """返回已缓存的 (rule, version)；ETag 优先，其次同路由缓存，找不到返回 None。"""
        with self.connect() as connection:
            if etag:
                row = connection.execute(
                    "SELECT rule, version FROM carriers WHERE etag = ? LIMIT 1", (etag,)
                ).fetchone()
                if row is None:
                    row = connection.execute(
                        "SELECT rule, version FROM source_cache WHERE etag = ? LIMIT 1", (etag,)
                    ).fetchone()
                if row is not None:
                    return row["rule"], row["version"]
            row = connection.execute(
                "SELECT rule, version FROM source_cache WHERE route = ? LIMIT 1", (route,)
            ).fetchone()
            if row is not None and row["rule"] is not None:
                return row["rule"], row["version"]
        return None

    def record_observation(self, run_id, route, status, result, etag):
        with self.connect() as connection:
            connection.execute(
                "INSERT OR IGNORE INTO observations(run_id, route, status, result, etag, seen_at)"
                " VALUES(?, ?, ?, ?, ?, ?)",
                (run_id, route, status, result, etag, utc_now()),
            )

    def count_versions(self):
        with self.connect() as connection:
            return connection.execute("SELECT count(*) AS total FROM rule_versions").fetchone()["total"]

    def count_carriers(self):
        with self.connect() as connection:
            return connection.execute("SELECT count(*) AS total FROM carriers").fetchone()["total"]

    # -- 历史选取 ---------------------------------------------------------------------

    def select_version(self, rule, effective_on, known_on):
        """按审核发生时的可知范围选取规则版本。

        可选版本必须同时满足：
        - published_at <= known_on（当时已经发布、可知）
        - effective_at <= effective_on（当时已经业务生效）
        取其中最高版本；没有可选版本时返回 None。
        """
        with self.connect() as connection:
            row = connection.execute(
                "SELECT version FROM rule_versions"
                " WHERE rule = ? AND published_at <= ? AND effective_at <= ?"
                " ORDER BY version DESC LIMIT 1",
                (rule, known_on, effective_on),
            ).fetchone()
            return None if row is None else row["version"]

    def version_rows(self, rule):
        with self.connect() as connection:
            return [
                dict(row)
                for row in connection.execute(
                    "SELECT rule, version, published_at, effective_at, text, scope"
                    " FROM rule_versions WHERE rule = ? ORDER BY version",
                    (rule,),
                )
            ]

    # -- 通知 -------------------------------------------------------------------------

    def create_notification(self, sku, rule, version, reason, run_id):
        with self.connect() as connection:
            before = connection.total_changes
            connection.execute(
                "INSERT OR IGNORE INTO notifications"
                "(sku, rule, version, reason, status, created_run, created_at)"
                " VALUES(?, ?, ?, ?, 'pending', ?, ?)",
                (sku, rule, version, reason, run_id, utc_now()),
            )
            return connection.total_changes - before

    def list_pending_notifications(self):
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT sku, rule, version, reason, status, created_run"
                " FROM notifications WHERE status = 'pending' ORDER BY id"
            )
            return [dict(row) for row in rows]


@dataclass(frozen=True)
class Scenario:
    name: str
    run_id: str
    sources: list
    effective_on: str
    known_on: str
    fixture_root: Path

    @classmethod
    def load(cls, path):
        raw = json.loads(Path(path).read_text())
        if not isinstance(raw.get("sources"), list) or not raw["sources"]:
            raise ValueError("场景文件缺少非空 sources")
        options = raw.get("options") or {}
        effective_on = parse_day(options.get("effective_on"), "options.effective_on")
        known_on = parse_day(options.get("known_on"), "options.known_on")
        fixture_root = raw.get("fixture_root")
        if not fixture_root:
            raise ValueError("场景文件缺少 fixture_root")
        root = Path(fixture_root)
        if not root.is_absolute():
            root = Path.cwd() / root
        return cls(
            name=str(raw.get("name") or "unnamed"),
            run_id=str(raw.get("run_id") or raw.get("name") or "run"),
            sources=list(raw["sources"]),
            effective_on=effective_on,
            known_on=known_on,
            fixture_root=root,
        )


def normalize_entry(value):
    if not isinstance(value, dict):
        raise ValueError("规则条目必须是 JSON 对象")
    required = ("rule", "version", "published_at", "effective_at", "format", "text", "scope")
    missing = [key for key in required if key not in value]
    if missing:
        raise ValueError(f"规则条目缺少字段: {', '.join(missing)}")
    if not isinstance(value["rule"], str) or not value["rule"].strip():
        raise ValueError("rule 必须是非空字符串")
    if not isinstance(value["version"], int) or isinstance(value["version"], bool):
        raise ValueError("version 必须是整数")
    parse_day(value["published_at"], "published_at")
    parse_day(value["effective_at"], "effective_at")
    if not isinstance(value["scope"], list):
        raise ValueError("scope 必须是数组")
    return {
        "rule": value["rule"],
        "version": value["version"],
        "published_at": value["published_at"],
        "effective_at": value["effective_at"],
        "format": str(value["format"]),
        "text": str(value["text"]),
        "scope": list(value["scope"]),
    }


def load_context(fixture_root):
    path = Path(fixture_root) / "context.json"
    if not path.is_file():
        return {"products": [], "audits": []}
    context = json.loads(path.read_text())
    return {
        "products": list(context.get("products", [])),
        "audits": list(context.get("audits", [])),
    }


def run_scenario(scenario, database):
    """执行一次采集批次，返回业务投影字典。可重复执行且幂等。"""
    store = PolicyStore(database)
    store.initialize()
    store.upsert_context(load_context(scenario.fixture_root))

    transport = FixtureTransport(scenario.fixture_root)
    observations = []
    for route in scenario.sources:
        response = transport.request(route)
        etag = response.headers.get("ETag")
        status = response.status
        if status == 200:
            entries = [normalize_entry(item) for item in decode(response.body)]
            for entry in entries:
                store.ingest_entry(entry, route, etag, scenario.run_id)
            result = RESULT_CAPTURED
        elif status == 304:
            # 只有已缓存同规则时才能确认“无变化”；否则只是缓存无法核实。
            result = RESULT_UNCHANGED if store.cached_rule(route, etag) else RESULT_UNKNOWN_CACHE
        elif status == 503:
            # 来源暂时不可用不等于无变化：只记录观察，绝不删除既有版本。
            result = RESULT_UNAVAILABLE
        else:
            result = RESULT_ERROR
        store.record_observation(scenario.run_id, route, status, result, etag)
        observations.append({"route": route, "status": status, "result": result, "etag": etag})

    notifications_created = generate_notifications(store, scenario)

    primary_rule = pick_primary_rule(store)
    selected = None
    if primary_rule is not None:
        selected = {
            "rule": primary_rule,
            "version": store.select_version(primary_rule, scenario.effective_on, scenario.known_on),
            "effective_on": scenario.effective_on,
            "known_on": scenario.known_on,
        }

    return {
        "run_id": scenario.run_id,
        "name": scenario.name,
        "selected": selected,
        "versions": store.count_versions(),
        "carriers": store.count_carriers(),
        "notifications": store.list_pending_notifications(),
        "notifications_created": notifications_created,
        "audits": store.list_audits(),
        "observations": observations,
    }


def generate_notifications(store, scenario):
    """追溯生效的新版本通知受影响商品；既有 audits 引用保持不变。

    条件（缺一不可）：
    - 新版本号高于该审核引用的版本；
    - effective_at <= 审核发生日（审核当时新版本已业务生效，即追溯生效）；
    - published_at <= known_on（当前可知范围内才能通知，尚未抓到的不通知）。
    每个 sku+rule+version 唯一，重复执行不产生新通知。
    """
    created = 0
    products = store.list_products()
    for audit in store.list_audits():
        for row in store.version_rows(audit["rule"]):
            if row["version"] <= audit["rule_version"]:
                continue
            if row["effective_at"] > audit["on"]:
                continue
            if row["published_at"] > scenario.known_on:
                continue
            scope = json.loads(row["scope"])
            for product in products:
                if product["category"] not in scope:
                    continue
                created += store.create_notification(
                    product["sku"], row["rule"], row["version"], REASON_RETROACTIVE, scenario.run_id
                )
    return created


def pick_primary_rule(store):
    with store.connect() as connection:
        row = connection.execute(
            "SELECT rule FROM audits ORDER BY id LIMIT 1"
        ).fetchone()
        if row is not None:
            return row["rule"]
        row = connection.execute("SELECT rule FROM rule_versions ORDER BY rule LIMIT 1").fetchone()
        return None if row is None else row["rule"]


def query_rule(database, rule, effective_on, known_on):
    """规则版本查询：复现某次审核发生时（effective_on）在某可知日（known_on）的判断。"""
    parse_day(effective_on, "effective_on")
    parse_day(known_on, "known_on")
    store = PolicyStore(database)
    selected = store.select_version(rule, effective_on, known_on)
    eligible, excluded = [], []
    for row in store.version_rows(rule):
        reasons = []
        if row["published_at"] > known_on:
            reasons.append("published_after_known_on")
        if row["effective_at"] > effective_on:
            reasons.append("effective_after_effective_on")
        item = {"version": row["version"], "published_at": row["published_at"], "effective_at": row["effective_at"]}
        if reasons:
            item["reasons"] = reasons
            excluded.append(item)
        else:
            eligible.append(item)
    return {
        "rule": rule,
        "version": selected,
        "effective_on": effective_on,
        "known_on": known_on,
        "eligible_versions": eligible,
        "excluded_versions": excluded,
    }
