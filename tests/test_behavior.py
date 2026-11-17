import tempfile
import unittest
from pathlib import Path
from unittest import mock

from layoutlake.layouts import REGION_BIZ, UTC_DAY
from layoutlake.manifest import LayoutTable
from layoutlake.partitioning import business_day, business_day_bounds, normalize, utc_day
from layoutlake.scan import scan

WORKSPACE_ROOT = Path(__file__).resolve().parent.parent


def event(eid, occurred, region='东'):
    return {'event_id': eid, 'occurred': occurred, 'region': region}


# 覆盖UTC午夜、业务日与UTC日不同、空地区、乌鲁木齐时区等情形。
ROWS = [
    event('E1', '2026-10-31T23:30:00+00:00', '东'),          # UTC日10-31，北京业务日11-01
    event('E2', '2026-11-01T00:30:00+00:00', None),          # 空地区：无业务日
    event('E3', '2026-11-02T01:00:00+00:00', '西'),          # 乌鲁木齐业务日11-02
    event('E4', '2026-10-31T17:00:00+00:00', '东'),          # 北京业务日11-01，UTC日10-31
    event('E5', '2026-11-01T19:30:00+00:00', '西'),          # 乌鲁木齐业务日11-01
]
SPAN = ('2026-10-31T23:00:00+00:00', '2026-11-01T01:00:00+00:00')


class WorkspaceTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.directory = self.temporary.name
        self.resources = []

    def open(self, factory=LayoutTable):
        resource = factory(self.directory)
        self.resources.append(resource)
        return resource

    def tearDown(self):
        for resource in reversed(self.resources):
            resource.close()
        self.temporary.cleanup()

    def build_legacy(self, batch='B1', rows=None):
        table = self.open()
        table.append(batch, ROWS if rows is None else rows)
        version = table.publish()
        return table, version


class SemanticsTest(WorkspaceTest):
    def test_date_normalization_unchanged(self):
        self.assertEqual(utc_day('2026-11-01T07:30:00+08:00'), '2026-10-31')
        with self.assertRaises(ValueError):
            normalize(event('X', '2026-11-01T01:00:00'))

    def test_business_day_uses_registered_zone_only(self):
        # 同一UTC瞬间，两个地区业务日可能不同；业务日不是UTC日历日。
        self.assertEqual(business_day('2026-10-31T23:30:00+00:00', '东'), '2026-11-01')
        # 乌鲁木齐为UTC+6：UTC 23:30对应本地次日05:30，业务日同样进入11-01。
        self.assertEqual(business_day('2026-10-31T23:30:00+00:00', '西'), '2026-11-01')
        # UTC 17:30对应乌鲁木齐本地23:30，仍属当天，体现与北京的口径差异。
        self.assertEqual(business_day('2026-10-31T17:30:00+00:00', '西'), '2026-10-31')
        self.assertEqual(business_day('2026-10-31T17:30:00+00:00', '东'), '2026-11-01')
        self.assertIsNone(business_day('2026-11-01T00:30:00+00:00', None))
        with self.assertRaises(ValueError):
            business_day('2026-11-01T00:30:00+00:00', '未登记地区')

    def test_business_day_bounds_independent_of_machine_zone(self):
        start, end = business_day_bounds('2026-11-01', '东')
        self.assertEqual(start.isoformat(), '2026-10-31T16:00:00+00:00')
        self.assertEqual(end.isoformat(), '2026-11-01T16:00:00+00:00')
        start, end = business_day_bounds('2026-11-01', '西')
        self.assertEqual(start.isoformat(), '2026-10-31T18:00:00+00:00')

    def test_business_day_ignores_machine_local_timezone(self):
        # 业务日只允许来自regions.json登记的时区；在不同机器TZ下子进程结果必须一致。
        import os
        import subprocess
        import sys
        code = (
            "from layoutlake.partitioning import business_day,business_day_bounds;"
            "print(business_day('2026-10-31T23:30:00+00:00','东'));"
            "print(business_day_bounds('2026-11-01','东')[0].isoformat())")
        outputs = set()
        env_base = dict(os.environ)
        env_base.pop('TZ', None)
        for zone in ('UTC', 'America/Los_Angeles', 'Asia/Tokyo'):
            env = dict(env_base, TZ=zone)
            completed = subprocess.run([sys.executable, '-c', code], capture_output=True,
                                       text=True, env=env, cwd=WORKSPACE_ROOT, check=True)
            outputs.add(completed.stdout.strip())
        self.assertEqual(outputs, {'2026-11-01\n2026-10-31T16:00:00+00:00'})

    def test_occurred_payload_never_rewritten_by_layout(self):
        table, version = self.build_legacy()
        from layoutlake.parquet import query as read_parquet
        legacy_rows = []
        for item in table.files(version):
            legacy_rows.extend(read_parquet([item['path']]))
        table.migrate_pending()
        new_version = table.publish(REGION_BIZ)
        new_rows = []
        for item in table.files(new_version):
            new_rows.extend(read_parquet([item['path']]))
        self.assertEqual(sorted(r['event_id'] for r in legacy_rows),
                         sorted(r['event_id'] for r in new_rows))
        payload_by_id = {r['event_id']: r for r in new_rows}
        for old in legacy_rows:
            self.assertEqual(payload_by_id[old['event_id']]['occurred'], old['occurred'])
            self.assertEqual(payload_by_id[old['event_id']]['utc_day'], old['utc_day'])


class LegacyCompatibilityTest(WorkspaceTest):
    def test_default_layout_remains_utc_day(self):
        table = self.open()
        self.assertEqual(table.effective_layout(), UTC_DAY)
        table.append('B1', ROWS)
        self.assertTrue(all(item['layout'] == UTC_DAY for item in table.legacy_files()))

    def test_old_version_immutable_after_switch_and_migration(self):
        table, old_version = self.build_legacy()
        old_files = table.files(old_version)
        table.migrate_pending()
        table.publish(REGION_BIZ)
        table.append('B2', [event('E9', '2026-11-03T00:00:00+00:00', '东')])
        table.publish()
        reopened = self.open()
        self.assertEqual([item['id'] for item in reopened.files(old_version)],
                         [item['id'] for item in old_files])
        self.assertEqual(reopened.version_layout(old_version), UTC_DAY)
        # 旧文件物理上仍然存在，长导出按固定版本读取不受迁移影响。
        for item in reopened.files(old_version):
            self.assertTrue(__import__('pathlib').Path(item['path']).is_file())

    def test_legacy_scan_behavior_preserved(self):
        table, version = self.build_legacy(rows=[
            event('E1', '2026-10-31T23:30:00+00:00'),
            event('E2', '2026-11-01T00:30:00+00:00'),
            event('E3', '2026-11-02T01:00:00+00:00')])
        result = scan(table, version, *SPAN)
        self.assertEqual([r['event_id'] for r in result['rows']], ['E1', 'E2'])
        self.assertEqual(len(result['selected']), 2)
        self.assertEqual(len(result['skipped']), 1)

    def test_null_region_filter_and_exclusive_end(self):
        table, version = self.build_legacy(rows=[
            event('E1', '2026-10-31T23:30:00+00:00', None),
            event('E2', '2026-11-01T00:00:00+00:00', '东')])
        result = scan(table, version, '2026-10-31T00:00:00+00:00',
                      '2026-11-01T00:00:00+00:00', [None])
        self.assertEqual([r['event_id'] for r in result['rows']], ['E1'])
        self.assertIsNone(result['rows'][0]['region'])
        # 空列表按契约不返回任何地区。
        empty = scan(table, version, *SPAN, [])
        self.assertEqual(empty['rows'], [])
        self.assertEqual(empty['selected'], [])


class MigrationTest(WorkspaceTest):
    def _migrated_table(self):
        table, old_version = self.build_legacy()
        table.migrate_pending()
        new_version = table.publish(REGION_BIZ)
        return table, old_version, new_version

    def test_migration_copies_without_deleting_or_rewriting(self):
        table, old_version, new_version = self._migrated_table()
        old_paths = {item['path'] for item in table.files(old_version)}
        new_items = table.files(new_version)
        self.assertTrue(all(item['layout'] == REGION_BIZ for item in new_items))
        # 新旧文件同时在盘上，路径集合互不相交。
        new_paths = {item['path'] for item in new_items}
        self.assertTrue(old_paths.isdisjoint(new_paths))
        self.assertTrue(all(__import__('pathlib').Path(p).is_file() for p in old_paths | new_paths))
        # 每个源文件都登记了血缘与行级校验和。
        state = table.migration_state()
        self.assertEqual({s['source_file'] for s in state},
                         {item['id'] for item in table.files(old_version)})
        relationships = table.file_relationships()
        self.assertEqual(len(relationships), len(state))

    def test_partial_migration_mixed_layout_version(self):
        table, old_version = self.build_legacy()
        table.migrate_pending(limit=1)
        mixed_version = table.publish(REGION_BIZ)
        items = table.files(mixed_version)
        self.assertEqual({item['layout'] for item in items}, {UTC_DAY, REGION_BIZ})
        wide = ('2026-10-01T00:00:00+00:00', '2026-12-01T00:00:00+00:00')
        # 混合布局版本必须能联合读取两种物理类型的parquet，结果与纯旧版本一致。
        legacy_rows = [r['event_id'] for r in scan(table, old_version, *wide)['rows']]
        mixed_rows = [r['event_id'] for r in scan(table, mixed_version, *wide)['rows']]
        self.assertEqual(legacy_rows, mixed_rows)
        self.assertEqual(scan(table, mixed_version, *wide)['duplicates_dropped'], [])

    def test_migration_resumes_after_partial_run(self):
        table, old_version = self.build_legacy()
        total = len(table.legacy_files())
        table.migrate_pending(limit=1)
        self.assertEqual(len(table.migration_state()), 1)
        # 重新打开catalog模拟进程中断后续跑。
        table.close()
        resumed = self.open()
        moved = resumed.migrate_pending()
        self.assertEqual(len(resumed.migration_state()), total)
        # 再次执行不产生重复副本，天然幂等。
        self.assertEqual(resumed.migrate_pending(), [])
        self.assertEqual(len(resumed.db.execute('SELECT * FROM files').fetchall()),
                         total + len(moved) + 1)
        version = resumed.publish(REGION_BIZ)
        result = scan(resumed, version, *SPAN)
        self.assertEqual(len(result['duplicates_dropped']), 0)

    def test_migration_checksum_matches_source_rows(self):
        table, old_version, _ = self._migrated_table()
        from layoutlake.parquet import query as read_parquet
        from layoutlake.storage import fingerprint
        for state in table.migration_state():
            source = table.db.execute('SELECT path FROM files WHERE id=?',
                                      (state['source_file'],)).fetchone()
            rows = read_parquet([source['path']])
            self.assertEqual(state['checksum'], fingerprint(rows))
            self.assertEqual(state['rows'], len(rows))

    def test_published_versions_never_contain_both_copies(self):
        table, old_version, new_version = self._migrated_table()
        old_ids = {item['id'] for item in table.files(old_version)}
        new_ids = {item['id'] for item in table.files(new_version)}
        self.assertTrue(old_ids.isdisjoint(new_ids))
        result = scan(table, new_version,
                      '2026-10-01T00:00:00+00:00', '2026-12-01T00:00:00+00:00')
        self.assertEqual(sorted(r['event_id'] for r in result['rows']),
                         sorted(r['event_id'] for r in ROWS))
        self.assertEqual(result['duplicates_dropped'], [])

    def test_defensive_dedup_for_duplicated_event_id(self):
        import json
        table, old_version, new_version = self._migrated_table()
        # 人工构造一个同时含原件与副本的版本，模拟异常清单：行级去重仍保证不重复。
        ids = [item['id'] for item in table.files(new_version)]
        source_id = table.db.execute('SELECT source_file FROM files WHERE source_file IS NOT NULL '
                                     'LIMIT 1').fetchone()[0]
        ids.append(source_id)
        table.db.execute('INSERT INTO versions(files,layout) VALUES(?,?)',
                         (json.dumps(sorted(ids)), REGION_BIZ))
        broken = table.db.execute('SELECT max(id) FROM versions').fetchone()[0]
        result = scan(table, broken, '2026-10-01T00:00:00+00:00', '2026-12-01T00:00:00+00:00')
        self.assertEqual(len(result['rows']), len({r['event_id'] for r in ROWS}))
        self.assertTrue(result['duplicates_dropped'])


class ScanPruningTest(WorkspaceTest):
    def setUp(self):
        super().setUp()
        table, old_version, new_version = MigrationTest._migrated_table(self)
        self.table, self.old_version, self.new_version = table, old_version, new_version

    def test_cross_midnight_results_identical_across_layouts(self):
        legacy = scan(self.table, self.old_version, *SPAN)
        moved = scan(self.table, self.new_version, *SPAN)
        self.assertEqual([r['event_id'] for r in legacy['rows']],
                         [r['event_id'] for r in moved['rows']])

    def test_new_layout_prunes_more_files_than_legacy(self):
        # 时间+地区组合查询：旧布局没有地区身份，时间相交的文件只能全读；
        # 新布局可以再按地区/业务日裁掉无关文件，而行级结果保持一致。
        legacy = scan(self.table, self.old_version, *SPAN, regions=['东'])
        moved = scan(self.table, self.new_version, *SPAN, regions=['东'])
        self.assertLess(len(moved['selected']), len(legacy['selected']))
        # 行级结果仍完全一致：裁剪减少的是无关读取，不是符合条件的行。
        self.assertEqual([r['event_id'] for r in legacy['rows']],
                         [r['event_id'] for r in moved['rows']])
        # 裁剪必须给出逐文件理由，而不是关掉裁剪。
        self.assertTrue(all(d['time_reason'] and d['region_reason']
                            for d in moved['decisions']))
        skipped = [d for d in moved['decisions'] if not d['selected']]
        self.assertTrue(any('不相交' in d['time_reason'] or '不在筛选集合' in d['region_reason']
                            for d in skipped))

    def test_null_region_file_is_conservatively_time_scanned(self):
        # 空地区文件无业务日：即使查询窗口按UTC看很窄，也不能凭时间裁掉它。
        moved = scan(self.table, self.new_version, *SPAN)
        null_decisions = [d for d in moved['decisions'] if d['region'] is None]
        self.assertEqual(len(null_decisions), 1)
        self.assertTrue(null_decisions[0]['selected'])
        self.assertIn('保守读取', null_decisions[0]['time_reason'])
        # 行级过滤仍然精确：窗口外的空地区行不返回。
        self.assertTrue(all(r['region'] is not None or
                            '2026-10-31T23:00:00+00:00' <= r['occurred'] < '2026-11-01T01:00:00+00:00'
                            for r in moved['rows']))

    def test_region_filter_prunes_new_layout_but_keeps_legacy_candidates(self):
        wide = ('2026-10-31T16:30:00+00:00', '2026-11-01T02:00:00+00:00')
        moved = scan(self.table, self.new_version, *wide, regions=['东'])
        self.assertTrue(all(d['selected'] for d in moved['decisions'] if d['region'] == '东'))
        self.assertTrue(all(not d['selected'] for d in moved['decisions']
                            if d['region'] not in ('东', None)))
        legacy = scan(self.table, self.old_version, *wide, regions=['东'])
        # 旧文件没有地区身份，时间相交的候选全部保守保留，地区裁剪只能交给行级过滤。
        legacy_time_kept = [d for d in legacy['decisions'] if d['selected']]
        self.assertTrue(all('保守读取' in d['region_reason'] for d in legacy_time_kept))
        self.assertEqual({r['event_id'] for r in legacy['rows']},
                         {r['event_id'] for r in moved['rows']})

    def test_region_filter_including_null(self):
        moved = scan(self.table, self.new_version,
                     '2026-10-01T00:00:00+00:00', '2026-12-01T00:00:00+00:00',
                     regions=['东', None])
        self.assertTrue({r['region'] for r in moved['rows']} <= {'东', None})
        chosen = {d['region'] for d in moved['decisions'] if d['selected']}
        self.assertIn(None, chosen)
        self.assertIn('东', chosen)
        self.assertNotIn('西', chosen)

    def test_wide_window_reads_everything_and_matches_contract(self):
        moved = scan(self.table, self.new_version,
                     '2026-10-01T00:00:00+00:00', '2026-12-01T00:00:00+00:00')
        self.assertEqual(sorted(r['event_id'] for r in moved['rows']),
                         sorted(r['event_id'] for r in ROWS))
        self.assertEqual(moved['skipped'], [])


class PublishAtomicityTest(WorkspaceTest):
    def test_switch_and_snapshot_commit_or_rollback_together(self):
        table, old_version = self.build_legacy()
        versions_before = table.db.execute('SELECT count(*) FROM versions').fetchone()[0]
        real_visible = table._visible_file_ids
        with mock.patch.object(LayoutTable, '_visible_file_ids',
                               side_effect=RuntimeError('发布中断')):
            with self.assertRaises(RuntimeError):
                table.publish(REGION_BIZ)
        # 事务回滚：生效布局没有被偷偷换掉，新版本没有留下半条记录。
        self.assertEqual(table.effective_layout(), UTC_DAY)
        self.assertEqual(table.db.execute('SELECT count(*) FROM versions').fetchone()[0],
                         versions_before)
        # 旧版本继续可读，且不会出现按新目录猜分区的状态。
        result = scan(table, old_version, *SPAN)
        self.assertTrue(result['rows'])
        self.assertEqual(table.version_layout(old_version), UTC_DAY)
        # 中断后重新发布成功。
        new_version = table.publish(REGION_BIZ)
        self.assertEqual(table.effective_layout(), REGION_BIZ)
        self.assertEqual(table.version_layout(new_version), REGION_BIZ)

    def test_new_writes_land_in_current_layout_only(self):
        table, _ = self.build_legacy()
        table.publish(REGION_BIZ)
        table.append('B2', [event('E8', '2026-11-02T03:00:00+00:00', '东')])
        fresh = [dict(r) for r in table.db.execute(
            "SELECT * FROM files WHERE batch='B2'")]
        self.assertTrue(fresh)
        self.assertTrue(all(r['layout'] == REGION_BIZ and r['source_file'] is None
                            for r in fresh))
        self.assertEqual(fresh[0]['biz_day'], '2026-11-02')

    def test_replay_conflict_and_idempotent_append(self):
        table = self.open()
        table.append('B1', ROWS)
        self.assertFalse(table.append('B1', ROWS))
        with self.assertRaises(ValueError):
            table.append('B1', [event('E3', '2026-11-02T01:00:00+00:00')])


class ManifestAuthorityTest(WorkspaceTest):
    def test_parquet_files_share_a_stable_schema_with_null_region(self):
        # 回归：含NULL与非NULL region的文件联合读取时，列类型必须一致可联合。
        from layoutlake.parquet import query as read_parquet
        table = self.open()
        table.append('B1', [
            event('E1', '2026-11-01T00:00:00+00:00', '东'),
            event('E2', '2026-11-01T01:00:00+00:00', None),
            event('E3', '2026-11-02T01:00:00+00:00', '西')])
        version = table.publish()
        rows = read_parquet([item['path'] for item in table.files(version)])
        self.assertEqual(sorted(r['event_id'] for r in rows), ['E1', 'E2', 'E3'])
        self.assertIsNone(next(r for r in rows if r['event_id'] == 'E2')['region'])

    def test_directory_names_never_drive_partitioning(self):
        table, old_version, new_version = MigrationTest._migrated_table(self)
        target = table.files(new_version)[0]
        # 把新布局文件搬到一个旧布局风格的诱饵目录：清单身份不变，裁剪与读取不变。
        from pathlib import Path
        original = Path(target['path'])
        decoy_dir = table.root / 'files' / 'utc_day=1999-01-01'
        decoy_dir.mkdir(parents=True, exist_ok=True)
        decoy = decoy_dir / original.name
        original.rename(decoy)
        table.db.execute('UPDATE files SET path=? WHERE id=?', (str(decoy), target['id']))
        table.db.commit()
        result = scan(table, new_version,
                      '2026-10-01T00:00:00+00:00', '2026-12-01T00:00:00+00:00')
        self.assertEqual(sorted(r['event_id'] for r in result['rows']),
                         sorted(r['event_id'] for r in ROWS))
        decision = next(d for d in result['decisions'] if d['file'] == target['id'])
        self.assertEqual(decision['layout'], REGION_BIZ)

    def test_catalog_reopen_picks_up_extended_schema(self):
        table, _ = self.build_legacy()
        table.close()
        reopened = self.open()
        columns = {r[1] for r in reopened.db.execute('PRAGMA table_info(files)')}
        self.assertIn('source_file', columns)
        self.assertIn('biz_day', columns)
        reopened.migrate_pending()
        self.assertTrue(reopened.migration_state())


if __name__ == '__main__':
    unittest.main()
