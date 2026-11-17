from .layouts import REGION_BIZ
from .manifest import LayoutTable
from .scan import scan


def _summarize(result):
    return {'event_ids':[row['event_id'] for row in result['rows']],
            'selected':result['selected'],'skipped':result['skipped']}


def run(root):
    table=LayoutTable(root)
    report={}
    try:
        # 1) 历史写入沿用utc_day布局，发布固定版本，旧读者始终按该版本完成。
        table.append('活动A',[
            {'event_id':'E1','occurred':'2026-10-31T23:30:00+00:00','region':'东'},
            {'event_id':'E2','occurred':'2026-11-01T00:30:00+00:00','region':None},
            {'event_id':'E3','occurred':'2026-11-02T01:00:00+00:00','region':'西'}])
        old_version=table.publish()
        query_span=('2026-10-31T23:00:00+00:00','2026-11-01T01:00:00+00:00')
        report['legacy_version']={'id':old_version,'layout':table.version_layout(old_version),
                                  'scan':_summarize(scan(table,old_version,*query_span))}

        # 2) 切换布局与发布新版本同一事务；之后的新写入按region_biz落文件。
        table.append('活动B',[
            {'event_id':'E4','occurred':'2026-11-01T02:00:00+00:00','region':'东'}])
        new_version=table.publish(REGION_BIZ)
        report['after_switch']={'effective_layout':table.effective_layout(),
                                'version':new_version,
                                'version_layout':table.version_layout(new_version)}

        # 3) 只复制不重写的渐进迁移，按源文件逐个提交，中断可续。
        first=table.migrate_pending(limit=1)
        resumed=table.migrate_pending()
        migrated_version=table.publish()
        report['migration']={'first_batch':first,'resumed':resumed,
                             'state':table.migration_state(),
                             'relationships':table.file_relationships()}

        # 4) 同一查询跨布局结果一致；新布局按地区/业务日裁掉更多无关文件。
        legacy_scan=scan(table,old_version,*query_span)
        migrated_scan=scan(table,migrated_version,*query_span)
        report['cross_check']={
            'legacy':_summarize(legacy_scan),
            'migrated':_summarize(migrated_scan),
            'same_event_ids':([r['event_id'] for r in legacy_scan['rows']]
                              ==[r['event_id'] for r in migrated_scan['rows']]),
            'migrated_decisions':migrated_scan['decisions'],
            'duplicates_dropped':migrated_scan['duplicates_dropped']}

        # 5) 空地区筛选：空地区文件无业务日，时间维度保守读取，行级过滤兜底。
        null_scan=scan(table,migrated_version,*query_span,regions=[None])
        report['null_region_scan']=_summarize(null_scan)
        return report
    finally:
        table.close()
