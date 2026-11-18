"""解析外部实体名称与内部分析主体。

别名带 linked_at：归属纠正以新映射的形式登记其生效/获知时间，
as_of 解析只看到当时已经建立的映射，从而让历史月报可复现。
"""
from .store import timestamp

class EntityDirectory:
    def __init__(self, connection):
        self.connection = connection
        connection.execute("""CREATE TABLE IF NOT EXISTS entities (
            entity_id TEXT PRIMARY KEY, label TEXT NOT NULL, group_id TEXT NOT NULL)""")
        connection.execute("""CREATE TABLE IF NOT EXISTS aliases (
            source TEXT NOT NULL, external_id TEXT NOT NULL, entity_id TEXT NOT NULL,
            linked_at TEXT NOT NULL,
            PRIMARY KEY(source, external_id), FOREIGN KEY(entity_id) REFERENCES entities(entity_id))""")
        columns = {row[1] for row in connection.execute("PRAGMA table_info(aliases)").fetchall()}
        if columns and "linked_at" not in columns:
            connection.execute(
                "ALTER TABLE aliases ADD COLUMN linked_at TEXT NOT NULL DEFAULT '0001-01-01T00:00:00+00:00'")

    def register(self, entity_id, label, group_id):
        if not all(isinstance(v, str) and v.strip() for v in (entity_id,label,group_id)):
            raise ValueError("实体信息不能为空")
        prior = self.connection.execute("SELECT label,group_id FROM entities WHERE entity_id=?", (entity_id,)).fetchone()
        if prior:
            if tuple(prior) != (label, group_id):
                raise ValueError("实体身份已登记为其他内容")
            return False
        self.connection.execute("INSERT INTO entities VALUES(?,?,?)", (entity_id,label,group_id))
        return True

    def link(self, source, external_id, entity_id, linked_at=None):
        exists = self.connection.execute("SELECT 1 FROM entities WHERE entity_id=?", (entity_id,)).fetchone()
        if not exists:
            raise ValueError("未知分析主体")
        linked = timestamp(linked_at) if linked_at else "0001-01-01T00:00:00+00:00"
        prior = self.connection.execute(
            "SELECT entity_id,linked_at FROM aliases WHERE source=? AND external_id=?", (source,external_id)).fetchone()
        if prior:
            if prior[0] != entity_id:
                raise ValueError("外部身份与现有主体冲突")
            if prior[1] == linked:
                return False
            self.connection.execute(
                "UPDATE aliases SET linked_at=? WHERE source=? AND external_id=?",
                (linked, source, external_id))
            return True
        self.connection.execute(
            "INSERT INTO aliases VALUES(?,?,?,?)", (source,external_id,entity_id,linked))
        return True

    def resolve(self, source, external_id, as_of=None):
        if as_of is None:
            row = self.connection.execute(
                "SELECT entity_id FROM aliases WHERE source=? AND external_id=?",
                (source,external_id)).fetchone()
        else:
            cutoff = timestamp(as_of)
            row = self.connection.execute(
                "SELECT entity_id FROM aliases WHERE source=? AND external_id=? AND linked_at<=?",
                (source,external_id,cutoff)).fetchone()
        return row[0] if row else None

    def alias_count(self):
        return self.connection.execute("SELECT COUNT(*) FROM aliases").fetchone()[0]
