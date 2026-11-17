from datetime import datetime, timedelta

from .layouts import REGION_BIZ, UTC_DAY
from .parquet import query
from .partitioning import business_day_bounds, utc_day


def _parse(timestamp):
    instant=datetime.fromisoformat(timestamp)
    if instant.tzinfo is None:
        raise ValueError("查询需提供显式时区")
    return instant


def _time_overlaps(left,right,file_item):
    """返回(是否相交, 理由)。分区信息不足时返回相交，宁可多读也不提前裁掉候选行。"""
    layout=file_item['layout']
    if layout==UTC_DAY:
        first=utc_day(left.isoformat())
        last=utc_day((right-timedelta(microseconds=1)).isoformat())
        day=file_item['day']
        if first<=day<=last:
            return True,'UTC日期范围与半开时间区间相交'
        return False,f'UTC日{day}不在查询覆盖的{first}..{last}内'
    if layout==REGION_BIZ:
        if file_item['region'] is None:
            # 空地区没有已知业务日，时间维度无法证明不相交：保守读取。
            return True,'空地区无业务日，时间裁剪信息不足，保守读取'
        if file_item['biz_day'] is None:
            return True,'业务日缺失，时间裁剪信息不足，保守读取'
        try:
            day_left,day_right=business_day_bounds(file_item['biz_day'],file_item['region'])
        except ValueError:
            return True,'地区业务时区未登记，时间裁剪信息不足，保守读取'
        if left<day_right and day_left<right:
            return True,f"业务日{file_item['biz_day']}({file_item['region']})的UTC区间与查询相交"
        return False,(f"业务日{file_item['biz_day']}({file_item['region']})的UTC区间"
                      f"[{day_left.isoformat()},{day_right.isoformat()})与查询不相交")
    return True,f'未知布局{layout}，保守读取'


def _region_matches(regions,file_item):
    """返回(是否保留, 理由)。regions=None不限制；含None表示包含空地区；空列表不返回任何地区。"""
    if regions is None:
        return True,'不限制地区'
    if not regions:
        return False,'地区筛选为空列表，按契约不返回任何地区的行'
    layout=file_item['layout']
    if layout==REGION_BIZ:
        if file_item['region'] in regions:
            region_name='空地区' if file_item['region'] is None else f"地区{file_item['region']}"
            return True,f'{region_name}在筛选集合内'
        region_name='空地区' if file_item['region'] is None else f"地区{file_item['region']}"
        return False,f'{region_name}不在筛选集合内'
    # 旧布局文件不携带地区身份，目录名也不能证明内容范围：保守读取交给行级过滤。
    return True,'旧布局文件无地区分区信息，保守读取后行级过滤'


def scan(table,version,start,end,regions=None):
    left=_parse(start)
    right=_parse(end)
    if left>=right:
        raise ValueError("查询需提供有时区的正长度区间")
    files=table.files(version)
    selected=[]
    decisions=[]
    for item in files:
        keep_time,time_reason=_time_overlaps(left,right,item)
        keep_region,region_reason=_region_matches(regions,item)
        keep=keep_time and keep_region
        if keep:
            selected.append(item)
        decisions.append({'file':item['id'],'layout':item['layout'],
                          'region':item['region'],'biz_day':item['biz_day'],
                          'selected':keep,'time_reason':time_reason,
                          'region_reason':region_reason})
    rows=query([item['path'] for item in selected],'SELECT * FROM payload ORDER BY event_id')
    result=[]
    seen=set()
    duplicates=[]
    for row in rows:
        instant=datetime.fromisoformat(row['occurred'])
        if not (left<=instant<right):
            continue
        if regions is not None and row['region'] not in regions:
            continue
        event_id=row['event_id']
        if event_id in seen:
            # 正常版本每个逻辑文件只暴露一份；此去重是迁移异常/人工版本下的防线。
            duplicates.append(event_id)
            continue
        seen.add(event_id)
        result.append(row)
    return {'rows':result,
            'selected':[item['id'] for item in selected],
            'skipped':[item['id'] for item in files if item not in selected],
            'decisions':decisions,
            'duplicates_dropped':duplicates,
            'reason':'按各文件清单布局身份做候选裁剪，行级再做半开时间与地区精确过滤'}
