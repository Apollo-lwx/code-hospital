"""病案室：SQLite（aiosqlite）持久化（设计 §5.2）。

建表：cases / findings / reports / diagnoses（prescriptions 等随后续阶段增补，
迁移走 schema_version 表 + ALTER/CREATE IF NOT EXISTS 增量脚本）。
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import aiosqlite

from ..core.ids import new_id
from ..core.models import Case, Diagnosis, Finding, ReportRecord, Scope

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
    auto_advance INTEGER NOT NULL DEFAULT 0,
    pending_gate TEXT NOT NULL DEFAULT '',
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
    content_key TEXT NOT NULL DEFAULT '',
    context_anchor TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'new',
    origin TEXT NOT NULL DEFAULT 'initial',
    first_seen_round INTEGER NOT NULL DEFAULT 1,
    last_seen_round INTEGER NOT NULL DEFAULT 1,
    contract_id TEXT NOT NULL DEFAULT '',
    fixable INTEGER NOT NULL DEFAULT 0
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
CREATE TABLE IF NOT EXISTS diagnoses (
    id TEXT PRIMARY KEY,
    case_id TEXT NOT NULL REFERENCES cases(id),
    report_id TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    category TEXT NOT NULL,
    root_cause TEXT NOT NULL,
    severity TEXT NOT NULL,
    confidence REAL NOT NULL,
    fixability TEXT NOT NULL,
    history_refs_json TEXT NOT NULL DEFAULT '[]',
    referrals_json TEXT NOT NULL DEFAULT '[]',
    member_ids_json TEXT NOT NULL DEFAULT '[]'
);
CREATE INDEX IF NOT EXISTS idx_diagnoses_case ON diagnoses(case_id, fingerprint);
CREATE TABLE IF NOT EXISTS prescriptions (
    id TEXT PRIMARY KEY,
    case_id TEXT NOT NULL REFERENCES cases(id),
    status TEXT NOT NULL,
    risk_level TEXT NOT NULL,
    approved_by TEXT NOT NULL DEFAULT '',
    human_in_loop INTEGER NOT NULL DEFAULT 0,
    confirmation_channel TEXT NOT NULL DEFAULT '',
    created_at INTEGER NOT NULL,
    approved_at INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_prescriptions_case ON prescriptions(case_id, created_at);
CREATE TABLE IF NOT EXISTS prescription_items (
    id TEXT PRIMARY KEY,
    prescription_id TEXT NOT NULL REFERENCES prescriptions(id),
    seq INTEGER NOT NULL,
    diagnosis_id TEXT NOT NULL,
    target_file TEXT NOT NULL DEFAULT '',
    target_symbol TEXT NOT NULL DEFAULT '',
    method TEXT NOT NULL,
    instructions TEXT NOT NULL DEFAULT '',
    acceptance_json TEXT NOT NULL DEFAULT '{}',
    rollback TEXT NOT NULL,
    scope_paths_json TEXT NOT NULL DEFAULT '[]',
    scope_symbols_json TEXT NOT NULL DEFAULT '[]',
    risk TEXT NOT NULL,
    touches_manifest INTEGER NOT NULL DEFAULT 0,
    amended_from TEXT NOT NULL DEFAULT '',
    patch_token TEXT NOT NULL DEFAULT '',
    token_expires_at INTEGER NOT NULL DEFAULT 0,
    patch_attempts INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_rx_items ON prescription_items(prescription_id, seq);
CREATE TABLE IF NOT EXISTS doses (
    id TEXT PRIMARY KEY,
    item_id TEXT NOT NULL,
    round INTEGER NOT NULL,
    executor TEXT NOT NULL,
    patch_path TEXT NOT NULL DEFAULT '',
    snapshot_hash TEXT NOT NULL DEFAULT '',
    diff_stat TEXT NOT NULL DEFAULT '',
    result TEXT NOT NULL,
    error TEXT NOT NULL DEFAULT '',
    created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_doses_item ON doses(item_id, created_at);
CREATE TABLE IF NOT EXISTS nursing_logs (
    id TEXT PRIMARY KEY,
    case_id TEXT NOT NULL,
    item_id TEXT NOT NULL,
    event TEXT NOT NULL,
    detail_json TEXT NOT NULL DEFAULT '{}',
    verdict TEXT NOT NULL,
    created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_nursing_case ON nursing_logs(case_id, created_at);
"""

HOSPITAL_DIR = ".hospital"


def now_ms() -> int:
    return int(time.time() * 1000)


def hospital_root(project_root: str) -> Path:
    return Path(project_root) / HOSPITAL_DIR


def _json_list(row: aiosqlite.Row, key: str) -> list:
    if key not in row.keys():
        return []
    return json.loads(row[key] or "[]")


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
        await self._migrate()
        await self._conn.commit()

    async def _migrate(self) -> None:
        """存量库增量迁移（P0 期约定：ALTER/CREATE IF NOT EXISTS 幂等）。"""
        cur = await self.conn.execute("PRAGMA table_info(cases)")
        cols = {r["name"] for r in await cur.fetchall()}
        # v1.1 阶段确认门禁（设计 §4.11）：cases 增列
        if "auto_advance" not in cols:
            await self.conn.execute(
                "ALTER TABLE cases ADD COLUMN auto_advance INTEGER NOT NULL DEFAULT 0"
            )
        if "pending_gate" not in cols:
            await self.conn.execute(
                "ALTER TABLE cases ADD COLUMN pending_gate TEXT NOT NULL DEFAULT ''"
            )
        cur = await self.conn.execute("PRAGMA table_info(findings)")
        finding_cols = {r["name"] for r in await cur.fetchall()}
        if "content_key" not in finding_cols:
            await self.conn.execute(
                "ALTER TABLE findings ADD COLUMN content_key TEXT NOT NULL DEFAULT ''"
            )
        if "contract_id" not in finding_cols:
            await self.conn.execute(
                "ALTER TABLE findings ADD COLUMN contract_id TEXT NOT NULL DEFAULT ''"
            )
        if "fixable" not in finding_cols:
            await self.conn.execute(
                "ALTER TABLE findings ADD COLUMN fixable INTEGER NOT NULL DEFAULT 0"
            )
        await self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_findings_content ON findings(content_key)"
        )
        cur = await self.conn.execute("PRAGMA table_info(diagnoses)")
        diagnosis_cols = {r["name"] for r in await cur.fetchall()}
        if diagnosis_cols and "member_ids_json" not in diagnosis_cols:
            await self.conn.execute(
                "ALTER TABLE diagnoses ADD COLUMN member_ids_json TEXT NOT NULL DEFAULT '[]'"
            )
        for name, ddl in (
            ("specialty_id", "TEXT NOT NULL DEFAULT ''"),
            ("formulary_matched", "INTEGER NOT NULL DEFAULT 1"),
        ):
            if diagnosis_cols and name not in diagnosis_cols:
                await self.conn.execute(f"ALTER TABLE diagnoses ADD COLUMN {name} {ddl}")
        for name, ddl in (
            ("requested_specialties_json", "TEXT NOT NULL DEFAULT '[]'"),
            ("requested_instruments_json", "TEXT NOT NULL DEFAULT '[]'"),
            ("scan_orders_json", "TEXT NOT NULL DEFAULT '[]'"),
            ("instrument_notes_json", "TEXT NOT NULL DEFAULT '[]'"),
        ):
            if name not in cols:
                await self.conn.execute(f"ALTER TABLE cases ADD COLUMN {name} {ddl}")
        cur = await self.conn.execute("PRAGMA table_info(prescription_items)")
        item_cols = {r["name"] for r in await cur.fetchall()}
        for name, ddl in (
            ("formula_id", "TEXT NOT NULL DEFAULT ''"),
            ("parent_formula_id", "TEXT NOT NULL DEFAULT ''"),
            ("specialty_id", "TEXT NOT NULL DEFAULT ''"),
            ("scope_key", "TEXT NOT NULL DEFAULT ''"),
            ("scope_lines_json", "TEXT NOT NULL DEFAULT '[]'"),
            ("formulary_conflict", "INTEGER NOT NULL DEFAULT 0"),
            ("conflict_reason", "TEXT NOT NULL DEFAULT ''"),
        ):
            if item_cols and name not in item_cols:
                await self.conn.execute(f"ALTER TABLE prescription_items ADD COLUMN {name} {ddl}")

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
            " triage_level, status, round, max_rounds, auto_advance, pending_gate,"
            " requested_specialties_json, requested_instruments_json, scan_orders_json,"
            " instrument_notes_json, created_at, updated_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                case.id, case.project_root, case.title, case.scope_type,
                json.dumps(case.scope.model_dump(), ensure_ascii=False),
                case.triage_level, case.status, case.round, case.max_rounds,
                int(case.auto_advance), case.pending_gate,
                json.dumps(case.requested_specialties, ensure_ascii=False),
                json.dumps(case.requested_instruments, ensure_ascii=False),
                json.dumps(case.scan_orders, ensure_ascii=False),
                json.dumps(case.instrument_notes, ensure_ascii=False),
                case.created_at, case.updated_at,
            ),
        )
        await self.conn.commit()

    @staticmethod
    def _row_to_case(row: aiosqlite.Row) -> Case:
        keys = set(row.keys())
        return Case(
            id=row["id"], project_root=row["project_root"], title=row["title"],
            scope_type=row["scope_type"], scope=Scope(**json.loads(row["scope_json"])),
            triage_level=row["triage_level"], status=row["status"],
            round=row["round"], max_rounds=row["max_rounds"],
            auto_advance=bool(row["auto_advance"]) if "auto_advance" in keys else False,
            pending_gate=row["pending_gate"] if "pending_gate" in keys else "",
            requested_specialties=_json_list(row, "requested_specialties_json"),
            requested_instruments=_json_list(row, "requested_instruments_json"),
            scan_orders=_json_list(row, "scan_orders_json"),
            instrument_notes=_json_list(row, "instrument_notes_json"),
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

        cur = await self.conn.execute(
            "SELECT COUNT(*) AS n FROM prescription_items i"
            " JOIN prescriptions p ON p.id = i.prescription_id WHERE p.case_id=?",
            (case_id,),
        )
        items_total = (await cur.fetchone())["n"]
        cur = await self.conn.execute(
            "SELECT COUNT(*) AS n FROM prescription_items i"
            " JOIN prescriptions p ON p.id = i.prescription_id"
            " WHERE p.case_id=? AND i.status='applied'",
            (case_id,),
        )
        items_applied = (await cur.fetchone())["n"]
        cur = await self.conn.execute(
            "SELECT COUNT(*) AS n FROM nursing_logs WHERE case_id=? AND verdict='fail'",
            (case_id,),
        )
        nursing_fail = (await cur.fetchone())["n"]
        return {
            "findings": await _count("findings"),
            "diagnoses": await _count("diagnoses"),
            "prescriptions": await _count("prescriptions"),
            "items_applied": items_applied,
            "items_total": items_total,
            "nursing_fail": nursing_fail,
        }

    # ---------- findings ----------

    async def insert_findings(self, findings: list[Finding]) -> None:
        await self.conn.executemany(
            "INSERT INTO findings (id, case_id, report_id, tool, rule_id, severity,"
            " file_path, line_start, line_end, symbol, message, evidence, fingerprint,"
            " content_key, context_anchor, status, origin, first_seen_round, last_seen_round,"
            " contract_id, fixable)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [
                (
                    f.id, f.case_id, f.report_id, f.tool, f.rule_id, f.severity,
                    f.file_path, f.line_start, f.line_end, f.symbol, f.message,
                    f.evidence, f.fingerprint, f.content_key, f.context_anchor, f.status, f.origin,
                    f.first_seen_round, f.last_seen_round, f.contract_id, int(f.fixable),
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
        content_keys: list[str] | None = None,
    ) -> dict[str, list[dict]]:
        """历史复发查询。identity 与 content_key 都建索引项，调档优先看长相。"""
        history: dict[str, list[dict]] = {}
        wanted = [("fingerprint", fingerprints), ("content_key", content_keys or [])]

        def push(key: str, row: aiosqlite.Row) -> None:
            if not key:
                return
            refs = history.setdefault(key, [])
            if len(refs) >= per_fingerprint or any(x["case_id"] == row["case_id"] for x in refs):
                return
            refs.append({
                "case_id": row["case_id"],
                "outcome": row["status"] if row["status"] in ("discharged", "referred") else "",
                "rounds": row["rounds"],
                "method": "",
                "note": "",
            })

        for column, values in wanted:
            clean = [v for v in values if v]
            for i in range(0, len(clean), 500):
                chunk = clean[i: i + 500]
                marks = ",".join("?" for _ in chunk)
                cur = await self.conn.execute(
                    "SELECT f.fingerprint AS fp, f.content_key AS ck, c.id AS case_id,"
                    "       c.status AS status, c.round AS rounds, c.created_at AS created_at"
                    " FROM findings f JOIN cases c ON c.id = f.case_id"
                    f" WHERE c.project_root=? AND f.{column} IN ({marks}) AND f.case_id != ?"
                    " ORDER BY c.created_at DESC",
                    (project_root, *chunk, exclude_case_id),
                )
                for r in await cur.fetchall():
                    push(r["fp"], r)
                    push(r["ck"], r)
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

    async def update_finding(self, finding_id: str, **fields) -> None:
        if not fields:
            return
        sets = ", ".join(f"{k}=?" for k in fields)
        await self.conn.execute(
            f"UPDATE findings SET {sets} WHERE id=?", (*fields.values(), finding_id)
        )
        await self.conn.commit()

    async def list_patch_texts(self, case_id: str) -> list[str]:
        cur = await self.conn.execute(
            "SELECT d.patch_path AS path FROM doses d"
            " JOIN prescription_items i ON i.id = d.item_id"
            " JOIN prescriptions p ON p.id = i.prescription_id"
            " WHERE p.case_id=? AND d.patch_path != ''",
            (case_id,),
        )
        texts = []
        for row in await cur.fetchall():
            path = Path(row["path"])
            if path.is_file():
                texts.append(path.read_text(encoding="utf-8", errors="replace"))
        return texts

    async def clear_diagnoses(self, case_id: str) -> None:
        await self.conn.execute("DELETE FROM diagnoses WHERE case_id=?", (case_id,))
        await self.conn.commit()

    async def all_findings(self, case_id: str) -> list[Finding]:
        cur = await self.conn.execute(
            "SELECT * FROM findings WHERE case_id=? ORDER BY rowid ASC", (case_id,)
        )
        return [self._row_to_finding(r) for r in await cur.fetchall()]

    # ---------- diagnoses ----------

    async def insert_diagnoses(self, items: list[Diagnosis]) -> None:
        await self.conn.executemany(
            "INSERT INTO diagnoses (id, case_id, report_id, fingerprint, category,"
            " root_cause, severity, confidence, fixability, history_refs_json, referrals_json,"
            " member_ids_json, specialty_id, formulary_matched)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [
                (
                    d.id, d.case_id, d.report_id, d.fingerprint, d.category,
                    d.root_cause, d.severity, d.confidence, d.fixability,
                    json.dumps(d.history_refs, ensure_ascii=False),
                    json.dumps(d.referrals, ensure_ascii=False),
                    json.dumps(d.member_ids, ensure_ascii=False),
                    d.specialty_id, int(d.formulary_matched),
                )
                for d in items
            ],
        )
        await self.conn.commit()

    async def list_diagnoses(self, case_id: str) -> list[Diagnosis]:
        cur = await self.conn.execute(
            "SELECT * FROM diagnoses WHERE case_id=? ORDER BY rowid ASC", (case_id,)
        )
        out: list[Diagnosis] = []
        for r in await cur.fetchall():
            keys = set(r.keys())
            out.append(Diagnosis(
                id=r["id"], case_id=r["case_id"], report_id=r["report_id"],
                fingerprint=r["fingerprint"], category=r["category"],
                root_cause=r["root_cause"], severity=r["severity"],
                confidence=r["confidence"], fixability=r["fixability"],
                history_refs=json.loads(r["history_refs_json"] or "[]"),
                referrals=json.loads(r["referrals_json"] or "[]"),
                member_ids=json.loads(r["member_ids_json"] or "[]") if "member_ids_json" in keys else [],
                specialty_id=r["specialty_id"] if "specialty_id" in keys else "",
                formulary_matched=bool(r["formulary_matched"]) if "formulary_matched" in keys else True,
            ))
        return out

    # ---------- prescriptions ----------

    async def insert_prescription(self, header: dict, items: list[dict]) -> None:
        await self.conn.execute(
            "INSERT INTO prescriptions (id, case_id, status, risk_level, approved_by,"
            " human_in_loop, confirmation_channel, created_at, approved_at)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (
                header["id"], header["case_id"], header["status"], header["risk_level"],
                header.get("approved_by", ""), int(header.get("human_in_loop", False)),
                header.get("confirmation_channel", ""), header["created_at"],
                header.get("approved_at", 0),
            ),
        )
        await self.conn.executemany(
            "INSERT INTO prescription_items (id, prescription_id, seq, diagnosis_id,"
            " target_file, target_symbol, method, instructions, acceptance_json, rollback,"
            " scope_paths_json, scope_symbols_json, risk, touches_manifest, amended_from,"
            " status, formula_id, parent_formula_id, specialty_id, scope_key,"
            " scope_lines_json, formulary_conflict, conflict_reason)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [
                (
                    it["id"], header["id"], it["seq"], it["diagnosis_id"],
                    it["target_file"], it["target_symbol"], it["method"], it["instructions"],
                    json.dumps(it["acceptance"], ensure_ascii=False), it["rollback"],
                    json.dumps(it["scope_paths"], ensure_ascii=False),
                    json.dumps(it["scope_symbols"], ensure_ascii=False),
                    it["risk"], int(it["touches_manifest"]), it["amended_from"], it["status"],
                    it.get("formula_id", ""), it.get("parent_formula_id", ""),
                    it.get("specialty_id", ""), it.get("scope_key", ""),
                    json.dumps(it.get("scope_lines") or [], ensure_ascii=False),
                    int(it.get("formulary_conflict") or 0), it.get("conflict_reason") or "",
                )
                for it in items
            ],
        )
        await self.conn.commit()

    async def get_prescription(self, prescription_id: str) -> dict | None:
        cur = await self.conn.execute(
            "SELECT * FROM prescriptions WHERE id=?", (prescription_id,)
        )
        row = await cur.fetchone()
        if row is None:
            return None
        items = await self.list_prescription_items(row["id"])
        return {
            "id": row["id"], "case_id": row["case_id"], "status": row["status"],
            "risk_level": row["risk_level"], "approved_by": row["approved_by"],
            "human_in_loop": bool(row["human_in_loop"]),
            "confirmation_channel": row["confirmation_channel"],
            "created_at": row["created_at"], "approved_at": row["approved_at"],
            "items": items,
        }

    async def latest_prescription(self, case_id: str) -> dict | None:
        cur = await self.conn.execute(
            "SELECT * FROM prescriptions WHERE case_id=? ORDER BY created_at DESC LIMIT 1",
            (case_id,),
        )
        row = await cur.fetchone()
        if row is None:
            return None
        items = await self.list_prescription_items(row["id"])
        return {
            "id": row["id"], "case_id": row["case_id"], "status": row["status"],
            "risk_level": row["risk_level"], "approved_by": row["approved_by"],
            "human_in_loop": bool(row["human_in_loop"]),
            "confirmation_channel": row["confirmation_channel"],
            "created_at": row["created_at"], "approved_at": row["approved_at"],
            "items": items,
        }

    async def list_prescription_items(self, prescription_id: str) -> list[dict]:
        cur = await self.conn.execute(
            "SELECT * FROM prescription_items WHERE prescription_id=? ORDER BY seq ASC",
            (prescription_id,),
        )
        items = []
        for r in await cur.fetchall():
            items.append({
                "id": r["id"], "prescription_id": r["prescription_id"], "seq": r["seq"],
                "diagnosis_id": r["diagnosis_id"], "target_file": r["target_file"],
                "target_symbol": r["target_symbol"], "method": r["method"],
                "instructions": r["instructions"],
                "acceptance": json.loads(r["acceptance_json"] or "{}"),
                "rollback": r["rollback"],
                "scope_paths": json.loads(r["scope_paths_json"] or "[]"),
                "scope_symbols": json.loads(r["scope_symbols_json"] or "[]"),
                "risk": r["risk"], "touches_manifest": bool(r["touches_manifest"]),
                "amended_from": r["amended_from"], "status": r["status"],
                "patch_token": r["patch_token"],
                "token_expires_at": r["token_expires_at"],
                "patch_attempts": r["patch_attempts"],
                "formula_id": r["formula_id"] if "formula_id" in r.keys() else "",
                "parent_formula_id": r["parent_formula_id"] if "parent_formula_id" in r.keys() else "",
                "specialty_id": r["specialty_id"] if "specialty_id" in r.keys() else "",
                "scope_key": r["scope_key"] if "scope_key" in r.keys() else "",
                "scope_lines": json.loads(r["scope_lines_json"] or "[]") if "scope_lines_json" in r.keys() else [],
                "formulary_conflict": bool(r["formulary_conflict"]) if "formulary_conflict" in r.keys() else False,
                "conflict_reason": r["conflict_reason"] if "conflict_reason" in r.keys() else "",
            })
        return items

    async def update_prescription(self, prescription_id: str, **fields) -> None:
        if "human_in_loop" in fields:
            fields["human_in_loop"] = int(fields["human_in_loop"])
        sets = ", ".join(f"{k}=?" for k in fields)
        await self.conn.execute(
            f"UPDATE prescriptions SET {sets} WHERE id=?",
            (*fields.values(), prescription_id),
        )
        await self.conn.commit()

    async def update_prescription_item(self, item_id: str, **fields) -> None:
        if not fields:
            return
        sets = ", ".join(f"{k}=?" for k in fields)
        await self.conn.execute(
            f"UPDATE prescription_items SET {sets} WHERE id=?",
            (*fields.values(), item_id),
        )
        await self.conn.commit()

    async def insert_dose(self, dose: dict) -> None:
        await self.conn.execute(
            "INSERT INTO doses (id, item_id, round, executor, patch_path, snapshot_hash,"
            " diff_stat, result, error, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                dose["id"], dose["item_id"], dose["round"], dose["executor"],
                dose["patch_path"], dose["snapshot_hash"], dose["diff_stat"],
                dose["result"], dose["error"], dose["created_at"],
            ),
        )
        await self.conn.commit()

    async def latest_dose(self, item_id: str) -> dict | None:
        cur = await self.conn.execute(
            "SELECT id, item_id, round, executor, patch_path, snapshot_hash, diff_stat,"
            " result, error, created_at FROM doses WHERE item_id=?"
            " ORDER BY created_at DESC LIMIT 1",
            (item_id,),
        )
        row = await cur.fetchone()
        return dict(row) if row is not None else None

    async def insert_nursing_log(self, case_id: str, item_id: str, event: str, detail: dict, verdict: str) -> str:
        log_id = new_id("nlog")
        await self.conn.execute(
            "INSERT INTO nursing_logs (id, case_id, item_id, event, detail_json, verdict, created_at)"
            " VALUES (?,?,?,?,?,?,?)",
            (log_id, case_id, item_id, event, json.dumps(detail, ensure_ascii=False), verdict, now_ms()),
        )
        await self.conn.commit()
        return log_id

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

    async def update_report_summary(self, report_id: str, summary: str) -> None:
        await self.conn.execute(
            "UPDATE reports SET summary=? WHERE id=?", (summary, report_id)
        )
        await self.conn.commit()

    async def list_reports(self, case_id: str) -> list[ReportRecord]:
        cur = await self.conn.execute(
            "SELECT * FROM reports WHERE case_id=? ORDER BY created_at ASC", (case_id,)
        )
        return [ReportRecord(**{k: r[k] for k in ReportRecord.model_fields})
                for r in await cur.fetchall()]
