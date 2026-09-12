"""只读 SQLite 访问 substrate（plan2 §6）。

连接合同（不可放宽；plan2 §6.1）：
  * stdlib sqlite3，URI ``file:<path>?mode=ro`` + ``uri=True``；
  * ``PRAGMA query_only=ON``（纵深防御）；
  * busy_timeout 目标 25~50ms（上限 100ms）——DeskPet 绝不能因为 Agent
    正在写数据库而阻塞 monitor 数秒；
  * SELECT-only：非 SELECT 语句在入口即拒绝；
  * 绝不 journal_mode / checkpoint / VACUUM / 建表建索引 / 写路径；
  * 绝不 ``immutable=1`` 读取 live WAL DB；绝不 ``nolock=1``；
  * 打不开（WAL 异常/locked/corrupt）→ 返回失败诊断，由调用方保留
    last good 并标记 degraded；本模块绝不 repair（plan2 §6.1/§13）。

指纹（plan2 §6.2）：
  * (main_mtime_ns, main_size, wal_mtime_ns, wal_size)；指纹未变化时
    调用方应跳过 SQL（空闲时仅低频 safety refresh）。

schema capability（plan2 §6.3）：
  * PRAGMA user_version + table_info + sqlite_master 白名单表名检查；
    SQL 按存在的列拼接固定白名单字段，绝不捕获异常后无限猜 schema。
"""
import os
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

# busy_timeout 目标 25~50ms，硬上限 100ms（plan2 §6.1）
BUSY_TIMEOUT_MS_DEFAULT = 40
BUSY_TIMEOUT_MS_MAX = 100

_SELECT_PREFIXES = ("select", "with")
# schema 能力探测允许的 PRAGMA/查询（白名单；防调用方误传任意 SQL）
_MAX_SCHEMA_TABLES = 16


@dataclass(frozen=True)
class DbFingerprint:
    """数据库变更指纹（plan2 §6.2）；exists=False 表示文件不可 stat。"""
    exists: bool = False
    main_mtime_ns: int = 0
    main_size: int = 0
    wal_mtime_ns: int = 0
    wal_size: int = 0


def stat_fingerprint(db_path: str) -> DbFingerprint:
    """一次 os.stat 采样（main + -wal）；不含任何打开/读取。"""
    try:
        st = os.stat(db_path)
    except OSError:
        return DbFingerprint(exists=False)
    wal_mtime_ns = 0
    wal_size = 0
    try:
        wst = os.stat(str(db_path) + "-wal")
        wal_mtime_ns = wst.st_mtime_ns
        wal_size = wst.st_size
    except OSError:
        pass
    return DbFingerprint(
        exists=True,
        main_mtime_ns=st.st_mtime_ns,
        main_size=st.st_size,
        wal_mtime_ns=wal_mtime_ns,
        wal_size=wal_size,
    )


def fingerprints_differ(a: DbFingerprint, b: DbFingerprint) -> bool:
    return (a.exists != b.exists
            or a.main_mtime_ns != b.main_mtime_ns
            or a.main_size != b.main_size
            or a.wal_mtime_ns != b.wal_mtime_ns
            or a.wal_size != b.wal_size)


@dataclass
class SchemaCapabilities:
    """一次连接的 schema 能力（plan2 §6.3）。只记录白名单内表/列。"""
    user_version: int = 0
    tables: frozenset = frozenset()
    columns: dict = field(default_factory=dict)

    def has_table(self, name: str) -> bool:
        return name in self.tables

    def has_columns(self, table: str, needed) -> bool:
        cols = self.columns.get(table)
        if cols is None:
            return False
        return all(c in cols for c in needed)

    def columns_of(self, table: str) -> tuple:
        return self.columns.get(table, ())


class ReadOnlySqlite:
    """单连接、短事务、只读访问；指纹/能力探测由调用方编排。

    query() 只接受 SELECT/WITH 开头的语句并按 max_rows 截断；locked/
    corrupt/missing 全部体现为 query/open 返回 None/False + last_error，
    绝不抛出到 Monitor 循环，绝不重试写。
    """

    def __init__(self, path: str,
                 busy_timeout_ms: int = BUSY_TIMEOUT_MS_DEFAULT):
        self.path = str(path)
        self.busy_timeout_ms = min(
            BUSY_TIMEOUT_MS_MAX, max(1, int(busy_timeout_ms)))
        self._con: sqlite3.Connection | None = None
        self.last_error = ""
        self.query_count = 0
        self.busy_count = 0
        self.open_count = 0

    # ------------------------------------------------------------ 生命周期
    def open(self) -> bool:
        """只读打开 + query_only；失败置 last_error 返回 False。"""
        if self._con is not None:
            return True
        try:
            uri = Path(self.path).absolute().as_uri() + "?mode=ro"
            con = sqlite3.connect(
                uri, uri=True, timeout=self.busy_timeout_ms / 1000.0)
        except (sqlite3.Error, ValueError, OSError) as exc:
            self.last_error = f"open failed: {exc!r}"
            return False
        try:
            con.execute("PRAGMA query_only=ON")
            con.row_factory = sqlite3.Row
        except sqlite3.Error as exc:
            self.last_error = f"query_only failed: {exc!r}"
            try:
                con.close()
            except sqlite3.Error:
                pass
            return False
        self._con = con
        self.open_count += 1
        self.last_error = ""
        return True

    def close(self) -> None:
        con, self._con = self._con, None
        if con is not None:
            try:
                con.close()
            except sqlite3.Error:
                pass

    # ------------------------------------------------------------ 能力探测
    def read_schema(self, needed_tables) -> SchemaCapabilities | None:
        """白名单表/列能力探测（plan2 §6.3）；失败返回 None。"""
        con = self._con
        if con is None:
            return None
        tables = [str(t) for t in list(dict.fromkeys(needed_tables))
                 ][: _MAX_SCHEMA_TABLES]
        caps = SchemaCapabilities()
        try:
            row = con.execute("PRAGMA user_version").fetchone()
            caps.user_version = int(row[0]) if row else 0
            names = {
                str(r[0]) for r in con.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'")}
            caps.tables = frozenset(n for n in names if n in tables)
            for table in tables:
                if table not in caps.tables:
                    continue
                caps.columns[table] = tuple(
                    str(r[1]) for r in con.execute(f"PRAGMA table_info({table})"))
        except sqlite3.OperationalError as exc:
            self.busy_count += 1
            self.last_error = f"schema probe failed: {exc!r}"
            return None
        except sqlite3.Error as exc:
            self.last_error = f"schema probe failed: {exc!r}"
            return None
        self.last_error = ""
        return caps

    # ------------------------------------------------------------ 查询
    def query(self, sql: str, params: tuple = (),
              max_rows: int = 64) -> list | None:
        """有界 SELECT；非 SELECT / 失败返回 None（busy 计数分离）。"""
        con = self._con
        if con is None:
            self.last_error = "connection not open"
            return None
        if not sql.lstrip().lower().startswith(_SELECT_PREFIXES):
            self.last_error = "non-select statement rejected"
            return None
        limit = max(1, min(int(max_rows), 4096))
        try:
            cur = con.execute(sql, params)
            rows = cur.fetchmany(limit + 1)
        except sqlite3.OperationalError as exc:
            text = repr(exc)
            if "locked" in text or "busy" in text:
                self.busy_count += 1
            self.last_error = f"query failed: {text}"
            return None
        except sqlite3.Error as exc:
            self.last_error = f"query failed: {exc!r}"
            return None
        self.query_count += 1
        self.last_error = ""
        if len(rows) > limit:
            rows = rows[:limit]
        return rows
