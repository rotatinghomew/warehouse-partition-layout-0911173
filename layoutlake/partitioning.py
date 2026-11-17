import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

REGIONS_PATH = Path(__file__).resolve().parent.parent / 'regions.json'


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


_zone_cache={}


def region_zones(path=None):
    """登记地区->业务时区。只能来自regions.json，禁止从名称或机器本地时区推断。"""
    source=Path(path) if path is not None else REGIONS_PATH
    key=str(source)
    if key not in _zone_cache:
        with source.open(encoding='utf-8') as handle:
            data=json.load(handle)
        if not isinstance(data,dict) or not all(isinstance(name,str) and isinstance(zone,str)
                                                for name,zone in data.items()):
            raise ValueError("地区时区登记格式错误")
        _zone_cache[key]=dict(data)
    return dict(_zone_cache[key])


def _instant(timestamp):
    instant=timestamp if isinstance(timestamp,datetime) else datetime.fromisoformat(timestamp)
    if instant.tzinfo is None:
        raise ValueError("事件时间需要显式时区")
    return instant.astimezone(timezone.utc)


def business_day(timestamp,region,*,zones=None):
    """地区业务日：空地区没有已知业务日，返回None而不是猜测一个日期。"""
    if region is None:
        return None
    registry=region_zones() if zones is None else zones
    if region not in registry:
        raise ValueError(f"地区{region}未登记业务时区")
    instant=_instant(timestamp).astimezone(ZoneInfo(registry[region]))
    return instant.date().isoformat()


def business_day_bounds(day,region,*,zones=None):
    """业务日[当天00:00,次日00:00)在地区时区的边界，返回UTC半开区间端点。"""
    if region is None:
        raise ValueError("空地区没有业务日边界")
    registry=region_zones() if zones is None else zones
    if region not in registry:
        raise ValueError(f"地区{region}未登记业务时区")
    zone=ZoneInfo(registry[region])
    local_midnight=datetime.fromisoformat(day).replace(tzinfo=zone)
    start=local_midnight.astimezone(timezone.utc)
    end=(local_midnight+timedelta(days=1)).astimezone(timezone.utc)
    return start,end
