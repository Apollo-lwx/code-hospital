"""病案室：SQLite（aiosqlite）持久化（设计 §5.2）。

P0 建表子集：cases / findings / reports（diagnoses、prescriptions 等随 P1 增补，
迁移走 schema_version 表 + ALTER/CREATE IF NOT EXISTS 增量脚本）。
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import aiosqlite

from ..core.models import Case, Finding, ReportRecord, Scope

SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_version (version INTEGER PRIMARY KEY);
CREATE TABLE IF NOT EXISTS cases (
    id TEXT PRIMARY KEY,
    project_root TEXT NOT NULL,
    title TEXT NOT NULL DEFAULT '',
    scope_type TEXT NOT NULL,
    scope_json TEXT NOT NULL DEFAULT '{}',
    triage_level TEXT NOT NULL,
    status TEXT NOT NULL,
    round INTEGER NOT NULL DEFAULT 1,
    max_rounds INTEGER NOT NULL DEFAULT 3,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
);
-- 一仓一活跃病例（设计 §4.7）
CREATE UNIQUE INDEX IF NOT EXISTS idx_cases_active
    ON cases(project_root) WHERE status NOT IN ('discharged','referred');
CREATE TABLE IF NOT EXISTS findings (
    id TEXT PRIMARY KEY,
    case_id TEXT NOT NULL REFERENCES cases(id),
    report_id TEXT NOT NULL,
    tool TEXT NOT NULL,
    rule_id TEXT NOT NULL,
    severity TEXT NOT NULL,
    file_path TEXT NOT NULL,
    line_start INTEGER NOT NULL DEFAULT 0,
    line_end INTEGER NOT NULL DEFAULT 0,
    symbol TEXT NOT NULL DEFAULT '',
    message TEXT NOT NULL DEFAULT '',
    evidence TEXT NOT NULL DEFAULT '',
    fingerprint TEXT NOT NULL,
    context_anchor TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'new',
    origin TEXT NOT NULL DEFAULT 'initial',
    first_seen_round INTEGER NOT NULL DEFAULT 1,
    last_seen_round INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_findings_case_fp ON findings(case_id, fingerprint);
CREATE INDEX IF NOT EXISTS idx_findings_fp ON findings(fingerprint);
CREATE INDEX IF NOT EXISTS idx_findings_file ON findings(file_path);
CREATE TABLE IF NOT EXISTS reports (
    id TEXT PRIMARY KEY,
    case_id TEXT NOT NULL REFERENCES cases(id),
    stage TEXT NOT NULL,
    round INTEGER NOT NULL DEFAULT 1,
    md_path TEXT NOT NULL DEFAULT '',
    summary TEXT NOT NULL DEFAULT '',
    created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_reports_case ON reports(case_id, stage);
"""

HOSPITAL_DIR = ".hospital"


def now_ms() -> int:
    return int(time.time() * 1000)


def hospital_root(project_root: str) -> Path:
    return Path(project_root) / HOSPITAL_DIR


class Database:
    def __init__(self, project_root: str) -> None:
        self.project_root = project_root
        self.dir = hospital_root(project_root)
        self.db_path = self.dir / "hospital.db"
        self._conn: aiosqlite.Connection | None = None

    async def __aenter__(self) -> "Database":
        await self.connect()
        return self

    async def __aexit__(self, *exc) -> None:
        await self.close()

    async def connect(self) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        (self.dir / "reports").mkdir(exist_ok=True)
        (self.dir / "patches").mkdir(exist_ok=True)
        self._conn = await aiosqlite.connect(self.db_path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.executescript(SCHEMA)
        await self._conn.commit()

    async def close(self) -> None:
        if self._conn:
            await self._conn.close()
            self._conn = None

    @property
    def conn(self) -> aiosqlite.Connection:
        assert self._conn is not None, "Database 未 connect"
        return self._conn

    # ---------- cases ----------

    async def insert_case(self, case: Case) -> None:
        await self.conn.execute(
            "INSERT INTO cases (id, project_root, title, scope_type, scope_json,"
            " triage_level, status, round, max_rounds, created_at, updated_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                case.id, case.project_root, case.title, case.scope_type,
                json.dumps(case.scope.model_dump(), ensure_ascii=False),
                case.triage_level, case.status, case.round, case.max_rounds,
                case.created_at, case.updated_at,
            ),
        )
        await self.conn.commit()

    @staticmethod
    def _row_to_case(row: aiosqlite.Row) -> Case:
        return Case(
            id=row["id"], project_root=row["project_root"], title=row["title"],
            scope_type=row["scope_type"], scope=Scope(**json.loads(row["scope_json"])),
            triage_level=row["triage_level"], status=row["status"],
            round=row["round"], max_rounds=row["max_rounds"],
            created_at=row["created_at"], updated_at=row["updated_at"],
        )

    async def get_case(self, case_id: str) -> Case | None:
        cur = await self.conn.execute("SELECT * FROM cases WHERE id=?", (case_id,))
        row = await cur.fetchone()
        return self._row_to_case(row) if row else None

    async def get_active_case(self, project_root: str) -> Case | None:
        cur = await self.conn.execute(
            "SELECT * FROM cases WHERE project_root=? AND status NOT IN ('discharged','referred')"
            " ORDER BY created_at DESC LIMIT 1",
            (project_root,),
        )
        row = await cur.fetchone()
        return self._row_to_case(row) if row else None

    async def update_case(self, case_id: str, **fields) -> None:
        if not fields:
            return
        fields["updated_at"] = now_ms()
        sets = ", ".join(f"{k}=?" for k in fields)
        await self.conn.execute(
            f"UPDATE cases SET {sets} WHERE id=?", (*fields.values(), case_id)
        )
        await self.conn.commit()

    async def list_cases(
        self, project_root: str, status: str = "", page: int = 1, size: int = 50
    ) -> tuple[list[Case], int]:
        where = "WHERE project_root=?"
        args: list = [project_root]
        if status:
            where += " AND status=?"
            args.append(status)
        cur = await self.conn.execute(
            f"SELECT COUNT(*) AS n FROM cases {where}", args
        )
        total = (await cur.fetchone())["n"]
        cur = await self.conn.execute(
            f"SELECT * FROM cases {where} ORDER BY created_at DESC LIMIT ? OFFSET ?",
            (*args, size, (page - 1) * size),
        )
        return [self._row_to_case(r) for r in await cur.fetchall()], total

    async def case_counts(self, case_id: str) -> dict:
        async def _count(table: str) -> int:
            cur = await self.conn.execute(
                f"SELECT COUNT(*) AS n FROM {table} WHERE case_id=?", (case_id,)
            )
            return (await cur.fetchone())["n"]

        return {
            "findings": await _count("findings"),
            "diagnoses": 0,
            "prescriptions": 0,
            "items_applied": 0,
            "items_total": 0,
            "nursing_fail": 0,
        }

    # ---------- findings ----------

    async def insert_findings(self, findings: list[Finding]) -> None:
        await self.conn.executemany(
            "INSERT INTO findings (id, case_id, report_id, tool, rule_id, severity,"
            " file_path, line_start, line_end, symbol, message, evidence, fingerprint,"
            " context_anchor, status, origin, first_seen_round, last_seen_round)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [
                (
                    f.id, f.case_id, f.report_id, f.tool, f.rule_id, f.severity,
                    f.file_path, f.line_start, f.line_end, f.symbol, f.message,
                    f.evidence, f.fingerprint, f.context_anchor, f.status, f.origin,
                    f.first_seen_round, f.last_seen_round,
                )
                for f in findings
            ],
        )
        await self.conn.commit()

    @staticmethod
    def _row_to_finding(row: aiosqlite.Row) -> Finding:
        keys = set(row.keys())
        return Finding(**{k: row[k] for k in Finding.model_fields if k in keys})

    async def find_fingerprint_history(
        self,
        project_root: str,
        fingerprints: list[str],
        exclude_case_id: str,
        per_fingerprint: int = 3,
    ) -> dict[str, list[dict]]:
        """同指纹历史复发查询（设计 §4.8 初诊调档轻量版，P0 提前落地）。

        按指纹在本仓库全部历史病例（排除当前病例）中查同指纹发现，
        每个指纹按时间倒序最多 per_fingerprint 条（契约：top 3）。
        """
        history: dict[str, list[dict]] = {}
        if not fingerprints:
            return history
        for i in range(0, len(fingerprints), 500):  # IN 查询分块，防参数过多
            chunk = fingerprints[i: i + 500]
            marks = ",".join("?" for _ in chunk)
            cur = await self.conn.execute(
                f"SELECT f.fingerprint AS fp, c.id AS case_id, c.status AS status,"
                "       c.round AS rounds, c.created_at AS created_at"
                " FROM findings f JOIN cases c ON c.id = f.case_id"
                f" WHERE c.project_root=? AND f.fingerprint IN ({marks}) AND f.case_id != ?"
                " ORDER BY c.created_at DESC",
                (project_root, *chunk, exclude_case_id),
            )
            for r in await cur.fetchall():
                refs = history.setdefault(r["fp"], [])
                if len(refs) < per_fingerprint and all(
                    x["case_id"] != r["case_id"] for x in refs
                ):
                    refs.append({
                        "case_id": r["case_id"],
                        "outcome": r["status"] if r["status"] in ("discharged", "referred") else "",
                        "rounds": r["rounds"],
                        "method": "",
                        "note": "",
                    })
        return history

    async def list_findings(
        self,
        case_id: str,
        page: int = 1,
        size: int = 50,
        order_by: str = "severity desc",
    ) -> tuple[list[Finding], int]:
        order = {
            "severity desc": "CASE severity WHEN 'critical' THEN 0 WHEN 'major' THEN 1"
                             " WHEN 'minor' THEN 2 WHEN 'nit' THEN 3 ELSE 4 END ASC",
            "file_path": "file_path ASC",
        }.get(order_by, "rowid ASC")
        cur = await self.conn.execute(
            "SELECT COUNT(*) AS n FROM findings WHERE case_id=?", (case_id,)
        )
        total = (await cur.fetchone())["n"]
        cur = await self.conn.execute(
            f"SELECT * FROM findings WHERE case_id=? ORDER BY {order} LIMIT ? OFFSET ?",
            (case_id, size, (page - 1) * size),
        )
        return [self._row_to_finding(r) for r in await cur.fetchall()], total

    async def find_duplicate_cases(
        self, project_root: str, file_paths: list[str], limit: int = 5
    ) -> list[dict]:
        """病例查重（P0 实现 same_file 维度）：历史病例的 findings 命中本次 scope 文件。

        same_fingerprint / same_rule 维度的命中要等本轮 scan 后才有意义（P1 初诊调档）。
        """
        if not file_paths:
            return []
        marks = ",".join("?" for _ in file_paths)
        cur = await self.conn.execute(
            f"SELECT c.id AS case_id, c.title AS title, c.status AS status,"
            "       c.updated_at AS closed_at, COUNT(DISTINCT f.file_path) AS hits"
            " FROM findings f JOIN cases c ON c.id = f.case_id"
            f" WHERE c.project_root=? AND f.file_path IN ({marks})"
            " GROUP BY c.id ORDER BY hits DESC, closed_at DESC LIMIT ?",
            (project_root, *file_paths, limit),
        )
        rows = await cur.fetchall()
        return [
            {
                "case_id": r["case_id"],
                "title": r["title"],
                "outcome": "" if r["status"] not in ("discharged", "referred") else r["status"],
                "similarity": "same_file",
                "closed_at": r["closed_at"],
            }
            for r in rows
        ]

    # ---------- reports ----------

    async def insert_report(self, report: ReportRecord) -> None:
        await self.conn.execute(
            "INSERT INTO reports (id, case_id, stage, round, md_path, summary, created_at)"
            " VALUES (?,?,?,?,?,?,?)",
            (
                report.id, report.case_id, report.stage, report.round,
                report.md_path, report.summary, report.created_at,
            ),
        )
        await self.conn.commit()

    async def list_reports(self, case_id: str) -> list[ReportRecord]:
        cur = await self.conn.execute(
            "SELECT * FROM reports WHERE case_id=? ORDER BY created_at ASC", (case_id,)
        )
        return [ReportRecord(**{k: r[k] for k in ReportRecord.model_fields})
                for r in await cur.fetchall()]
