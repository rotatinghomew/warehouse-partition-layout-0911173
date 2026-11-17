from datetime import datetime, timedelta

from .parquet import query
from .partitioning import utc_day


def scan(table,version,start,end,regions=None):
    left=datetime.fromisoformat(start)
    right=datetime.fromisoformat(end)
    if left.tzinfo is None or right.tzinfo is None or left>=right:
        raise ValueError("查询需提供有时区的正长度区间")
    first=utc_day(start)
    last=utc_day((right-timedelta(microseconds=1)).isoformat())
    files=table.files(version)
    selected=[item for item in files if first<=item['day']<=last]
    rows=query([item['path'] for item in selected],'SELECT * FROM payload ORDER BY event_id')
    result=[]
    for row in rows:
        instant=datetime.fromisoformat(row['occurred'])
        if left<=instant<right and (regions is None or row['region'] in regions):
            result.append(row)
    return {'rows':result,'selected':[item['id'] for item in selected],
            'skipped':[item['id'] for item in files if item not in selected],
            'reason':'UTC日期范围与半开时间区间相交'}
