from .manifest import LayoutTable
from .scan import scan


def run(root):
    table=LayoutTable(root)
    try:
        table.append('活动A',[
            {'event_id':'E1','occurred':'2026-10-31T23:30:00+00:00','region':'东'},
            {'event_id':'E2','occurred':'2026-11-01T00:30:00+00:00','region':None},
            {'event_id':'E3','occurred':'2026-11-02T01:00:00+00:00','region':'西'}])
        return scan(table,table.publish(),'2026-10-31T23:00:00+00:00','2026-11-01T01:00:00+00:00')
    finally:
        table.close()
