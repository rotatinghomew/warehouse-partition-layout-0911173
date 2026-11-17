import json
from pathlib import Path

from .layouts import REGION_BIZ, UTC_DAY, group_rows, part_dir, require_layout
from .parquet import write_rows
from .partitioning import normalize
from .storage import atomic, connect, fingerprint

DDL="""
CREATE TABLE IF NOT EXISTS batches(name TEXT PRIMARY KEY,checksum TEXT);
CREATE TABLE IF NOT EXISTS files(id INTEGER PRIMARY KEY,batch TEXT,day TEXT,path TEXT,rows INTEGER);
CREATE TABLE IF NOT EXISTS keys(event_id TEXT PRIMARY KEY,batch TEXT);
CREATE TABLE IF NOT EXISTS versions(id INTEGER PRIMARY KEY,files TEXT,layout TEXT);
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY,value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS migration_state(source_file INTEGER PRIMARY KEY,
    migrated_files TEXT NOT NULL,rows INTEGER NOT NULL,checksum TEXT NOT NULL,
    finished_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')));
"""

# 旧catalog的files表只有五列，用幂等ALTER补齐，历史行保持原身份不变。
FILE_COLUMNS={
    'layout':"TEXT NOT NULL DEFAULT 'utc_day'",
    'region':'TEXT',
    'biz_day':'TEXT',
    'source_file':'INTEGER REFERENCES files(id)',
    'checksum':'TEXT',
}


def ensure_columns(database):
    existing={row[1] for row in database.execute('PRAGMA table_info(files)')}
    for name,declaration in FILE_COLUMNS.items():
        if name not in existing:
            database.execute(f'ALTER TABLE files ADD COLUMN {name} {declaration}')


class LayoutTable:
    def __init__(self,root):
        self.root=Path(root)
        self.db=connect(root,DDL)
        ensure_columns(self.db)
        self.db.commit()

    def close(self):
        self.db.close()

    # ---- 生效布局：只决定新写入和下一次发布，固定版本永不回头改 ----
    def effective_layout(self):
        row=self.db.execute("SELECT value FROM settings WHERE key='layout'").fetchone()
        return row[0] if row else UTC_DAY

    def set_layout(self,layout):
        require_layout(layout)
        with atomic(self.db):
            self.db.execute("INSERT INTO settings(key,value) VALUES('layout',?) "
                            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",(layout,))

    def _write_group(self,layout,key,group,batch,*,source_file=None):
        _layout,day,region,biz_day=key
        checksum=fingerprint(group)
        directory=self.root / part_dir(layout,day=day,region=region,biz_day=biz_day)
        path=write_rows(directory,group)
        cursor=self.db.execute(
            'INSERT INTO files(batch,day,path,rows,layout,region,biz_day,source_file,checksum) '
            'VALUES(?,?,?,?,?,?,?,?,?)',
            (batch,day,path,len(group),layout,region,biz_day,source_file,checksum))
        return cursor.lastrowid

    def append(self,batch,rows):
        layout=self.effective_layout()
        normalized=[normalize(row) for row in rows]
        checksum=fingerprint(normalized)
        with atomic(self.db):
            old=self.db.execute('SELECT checksum FROM batches WHERE name=?',(batch,)).fetchone()
            if old:
                if old[0]!=checksum:
                    raise ValueError("批次内容冲突")
                return False
            self.db.execute('INSERT INTO batches VALUES(?,?)',(batch,checksum))
            for key,group in sorted(group_rows(layout,normalized).items(),
                                    key=lambda item: tuple('' if part is None else part
                                                           for part in item[0])):
                file_id=self._write_group(layout,key,group,batch)
                for row in group:
                    self.db.execute('INSERT INTO keys VALUES(?,?)',(row['event_id'],batch))
            return True

    # ---- 迁移：只复制不重写，按源文件逐个提交，中断后可继续 ----
    def legacy_files(self):
        return [dict(row) for row in self.db.execute(
            'SELECT * FROM files WHERE layout=? ORDER BY id',(UTC_DAY,))]

    def pending_migrations(self):
        done={row[0] for row in self.db.execute('SELECT source_file FROM migration_state')}
        return [item for item in self.legacy_files() if item['id'] not in done]

    def migrate_file(self,file_id):
        source=self.db.execute('SELECT * FROM files WHERE id=?',(file_id,)).fetchone()
        if not source:
            raise KeyError(file_id)
        source=dict(source)
        if source['layout']!=UTC_DAY:
            raise ValueError("只有utc_day布局文件需要迁移")
        done=self.db.execute('SELECT 1 FROM migration_state WHERE source_file=?',(file_id,)).fetchone()
        if done:
            return []
        from .parquet import query as read_parquet
        rows=read_parquet([source['path']])
        # 复制出的行与源文件行级一致；业务日仅登记在清单中，不进载荷，事件时间含义不变。
        groups=group_rows(REGION_BIZ,rows)
        migrated=[]
        with atomic(self.db):
            for key,group in sorted(groups.items(),
                                    key=lambda item: tuple('' if part is None else part
                                                           for part in item[0])):
                _layout,_day,region,biz_day=key
                new_id=self._write_group(REGION_BIZ,key,group,source['batch'],source_file=file_id)
                migrated.append(new_id)
            self.db.execute(
                'INSERT INTO migration_state(source_file,migrated_files,rows,checksum) '
                'VALUES(?,?,?,?)',
                (file_id,json.dumps(migrated),len(rows),fingerprint(rows)))
        return migrated

    def migrate_pending(self,limit=None):
        """迁移所有待处理源文件；limit用于分批/中断演练，已完成的源文件自动跳过。"""
        moved=[]
        pending=self.pending_migrations()
        if limit is not None:
            pending=pending[:limit]
        for item in pending:
            moved.extend(self.migrate_file(item['id']))
        return moved

    def migration_state(self):
        return [dict(row) for row in self.db.execute(
            'SELECT * FROM migration_state ORDER BY source_file')]

    # ---- 发布：布局设置与版本快照同一事务，失败则已有版本原样可用 ----
    def _visible_file_ids(self):
        # 迁移只复制：源文件与副本同时在库，但同一批逻辑数据在任一版本中只暴露一份。
        # 已迁移的原件被其副本取代；未迁移原件与原生新布局文件照常可见。
        replaced={row[0] for row in self.db.execute(
            'SELECT DISTINCT source_file FROM files WHERE source_file IS NOT NULL')}
        originals=[row[0] for row in self.db.execute(
            'SELECT id FROM files WHERE source_file IS NULL ORDER BY id')
            if row[0] not in replaced]
        copies=[row[0] for row in self.db.execute(
            'SELECT id FROM files WHERE source_file IS NOT NULL ORDER BY id')]
        return sorted(originals+copies)

    def publish(self,layout=None):
        with atomic(self.db):
            if layout is not None:
                require_layout(layout)
                self.db.execute(
                    "INSERT INTO settings(key,value) VALUES('layout',?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value",(layout,))
            effective=self.effective_layout()
            ids=self._visible_file_ids()
            cursor=self.db.execute('INSERT INTO versions(files,layout) VALUES(?,?)',
                                   (json.dumps(ids),effective))
            version=cursor.lastrowid
        return version

    def files(self,version):
        row=self.db.execute('SELECT files,layout FROM versions WHERE id=?',(version,)).fetchone()
        if not row:
            raise KeyError(version)
        result=[]
        for identity in json.loads(row[0]):
            item=self.db.execute('SELECT * FROM files WHERE id=?',(identity,)).fetchone()
            if item is None:
                raise KeyError(identity)
            result.append(dict(item))
        return result

    def version_layout(self,version):
        row=self.db.execute('SELECT layout FROM versions WHERE id=?',(version,)).fetchone()
        if not row:
            raise KeyError(version)
        return row[0]

    def file_relationships(self):
        """文件关系视图：每个迁移源文件对应哪些新布局文件，供核对与续迁。"""
        return [dict(row) for row in self.db.execute(
            'SELECT s.id AS source_id,s.path AS source_path,s.rows AS source_rows,'
            "GROUP_CONCAT(f.id) AS migrated_ids,s.checksum AS source_checksum "
            'FROM files s JOIN files f ON f.source_file=s.id GROUP BY s.id ORDER BY s.id')]
