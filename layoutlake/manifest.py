import json
from collections import defaultdict
from pathlib import Path

from .partitioning import normalize
from .parquet import write_rows
from .storage import atomic, connect, fingerprint

DDL="""
CREATE TABLE IF NOT EXISTS batches(name TEXT PRIMARY KEY,checksum TEXT);
CREATE TABLE IF NOT EXISTS files(id INTEGER PRIMARY KEY,batch TEXT,day TEXT,path TEXT,rows INTEGER);
CREATE TABLE IF NOT EXISTS keys(event_id TEXT PRIMARY KEY,batch TEXT);
CREATE TABLE IF NOT EXISTS versions(id INTEGER PRIMARY KEY,files TEXT,layout TEXT);
"""


class LayoutTable:
    def __init__(self,root):
        self.root=Path(root)
        self.db=connect(root,DDL)

    def close(self):
        self.db.close()

    def append(self,batch,rows):
        normalized=[normalize(row) for row in rows]
        checksum=fingerprint(normalized)
        with atomic(self.db):
            old=self.db.execute('SELECT checksum FROM batches WHERE name=?',(batch,)).fetchone()
            if old:
                if old[0]!=checksum:
                    raise ValueError("批次内容冲突")
                return False
            groups=defaultdict(list)
            for row in normalized:
                groups[row['utc_day']].append(row)
            self.db.execute('INSERT INTO batches VALUES(?,?)',(batch,checksum))
            for day,group in sorted(groups.items()):
                path=write_rows(self.root/'files'/('utc_day='+day),group)
                self.db.execute('INSERT INTO files(batch,day,path,rows) VALUES(?,?,?,?)',(batch,day,path,len(group)))
                for row in group:
                    self.db.execute('INSERT INTO keys VALUES(?,?)',(row['event_id'],batch))
        return True

    def publish(self):
        with atomic(self.db):
            ids=[row[0] for row in self.db.execute('SELECT id FROM files ORDER BY id')]
            cursor=self.db.execute('INSERT INTO versions(files,layout) VALUES(?,?)',(json.dumps(ids),'utc_day'))
        return cursor.lastrowid

    def files(self,version):
        row=self.db.execute('SELECT files FROM versions WHERE id=?',(version,)).fetchone()
        if not row:
            raise KeyError(version)
        result=[]
        for identity in json.loads(row[0]):
            result.append(dict(self.db.execute('SELECT * FROM files WHERE id=?',(identity,)).fetchone()))
        return result
