import tempfile
import unittest


class WorkspaceTest(unittest.TestCase):
    def setUp(self):
        self.temporary=tempfile.TemporaryDirectory()
        self.directory=self.temporary.name
        self.resources=[]

    def open(self, factory):
        resource=factory(self.directory)
        self.resources.append(resource)
        return resource

    def tearDown(self):
        for resource in reversed(self.resources):
            resource.close()
        self.temporary.cleanup()

from layoutlake.manifest import LayoutTable
from layoutlake.partitioning import utc_day, normalize
from layoutlake.scan import scan


def activity(event='E1',occurred='2026-10-31T23:30:00+00:00',region='东'):
    return {'event_id':event,'occurred':occurred,'region':region}


class Behavior(WorkspaceTest):
    def test_date_normalization(self):
        self.assertEqual(utc_day('2026-11-01T07:30:00+08:00'),'2026-10-31')
        with self.assertRaises(ValueError):
            normalize(activity(occurred='2026-11-01T01:00:00'))

    def test_safe_midnight_scan(self):
        table=self.open(LayoutTable)
        table.append('B1',[activity(),activity('E2','2026-11-01T00:30:00+00:00'),activity('E3','2026-11-02T01:00:00+00:00')])
        result=scan(table,table.publish(),'2026-10-31T23:00:00+00:00','2026-11-01T01:00:00+00:00')
        self.assertEqual([row['event_id'] for row in result['rows']],['E1','E2'])
        self.assertEqual(len(result['selected']),2)
        self.assertEqual(len(result['skipped']),1)

    def test_null_region_and_exclusive_end(self):
        table=self.open(LayoutTable)
        table.append('B1',[activity(region=None),activity('E2','2026-11-01T00:00:00+00:00')])
        version=table.publish()
        result=scan(table,version,'2026-10-31T00:00:00+00:00','2026-11-01T00:00:00+00:00',[None])
        self.assertEqual(len(result['rows']),1)
        self.assertIsNone(result['rows'][0]['region'])

    def test_replay_and_snapshot(self):
        table=self.open(LayoutTable)
        table.append('B1',[activity()])
        old=table.publish()
        self.assertFalse(table.append('B1',[activity()]))
        table.append('B2',[activity('E2')])
        table.publish()
        reopened=self.open(LayoutTable)
        self.assertEqual(len(reopened.files(old)),1)
        with self.assertRaises(ValueError):
            table.append('B1',[activity('E3')])
