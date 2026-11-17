from pathlib import PurePosixPath
from urllib.parse import quote

from .partitioning import business_day, normalize

UTC_DAY='utc_day'
REGION_BIZ='region_biz'
LAYOUTS=(UTC_DAY,REGION_BIZ)
# 空地区在region_biz布局中的物理目录占位；清单中的region仍为None，含义不改变。
NULL_REGION_TOKEN='__null_region__'


def require_layout(layout):
    if layout not in LAYOUTS:
        raise ValueError(f"未知布局{layout}")
    return layout


def part_dir(layout,*,day=None,region=None,biz_day=None):
    """只负责生成写入目录；读取侧不解析目录名，一切以清单身份为准。"""
    require_layout(layout)
    if layout==UTC_DAY:
        return PurePosixPath('files') / ('utc_day='+day)
    region_part=NULL_REGION_TOKEN if region is None else 'region='+quote(region,safe='')
    if biz_day is None:
        day_part='biz_day=__null__'
    else:
        day_part='biz_day='+biz_day
    return PurePosixPath('files') / region_part / day_part


def partition_key(layout,row):
    """规范化行在指定布局下的分组键，键本身也是该文件可验证的分区身份。"""
    normalized=normalize(row) if 'utc_day' not in row else row
    if layout==UTC_DAY:
        return (UTC_DAY,normalized['utc_day'],None,None)
    region=normalized['region']
    return (REGION_BIZ,None,region,business_day(normalized['occurred'],region))


def group_rows(layout,rows):
    groups={}
    for row in rows:
        key=partition_key(layout,row)
        groups.setdefault(key,[]).append(row)
    return groups
