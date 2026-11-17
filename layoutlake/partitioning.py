from datetime import datetime, timezone


def utc_day(timestamp):
    instant=datetime.fromisoformat(timestamp)
    if instant.tzinfo is None:
        raise ValueError("事件时间需要显式时区")
    return instant.astimezone(timezone.utc).date().isoformat()


def normalize(row):
    if not isinstance(row['event_id'],str) or not row['event_id']:
        raise ValueError("活动标识为空")
    instant=datetime.fromisoformat(row['occurred'])
    if instant.tzinfo is None:
        raise ValueError("事件时间需要时区")
    if row['region'] is not None and not isinstance(row['region'],str):
        raise ValueError("地区类型错误")
    return dict(row,occurred=instant.astimezone(timezone.utc).isoformat(),utc_day=utc_day(row['occurred']))
