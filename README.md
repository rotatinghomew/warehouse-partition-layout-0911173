# 按活动日期分区的Parquet清单与查询

这是一个处理本地数据交换的仓湖项目。示例使用虚构机构和匿名合成记录，不连接业务平台。

## 数据契约

事件时间为有时区的ISO8601，存储规范化为UTC。现有物理分区为UTC自然日，查询区间左闭右开，地区筛选中的None代表空地区。清单而非目录遍历决定可见文件。文件选择只是候选裁剪，之后仍执行行级精确过滤，返回selected、skipped及理由用于核对读取范围。

## 代码职责

- `layoutlake/partitioning.py`：UTC事件规范化与自然日分区。
- `layoutlake/manifest.py`：批次、文件统计与固定版本。
- `layoutlake/scan.py`：候选文件选择及行级精确过滤。
- `layoutlake/storage.py`：本地事务、内容摘要和查询工具。

## 安装与运行

使用 Python 3.11。先执行 `python3 -m pip install -r requirements.txt`；依赖仅在准备环境时安装，运行与测试均可断网。

```sh
python3 -m compileall -q layoutlake tests
python3 -m unittest discover -s tests -v
python3 -m layoutlake
```

默认示例在独立临时目录执行，结束后删除目录。需要检查持久文件时可执行 `python3 -m layoutlake --root state`。测试自己创建临时目录，不依赖固定端口、外部服务或其他用例顺序。

## 容器

```sh
docker build -t partition-layout:local .
docker run --rm --network none partition-layout:local
docker run --rm --network none partition-layout:local python3 -m layoutlake
```

容器构建负责预装锁定依赖，启动不下载包。示例输出是真实接入、查询和发布的数据，不读取预制输出。测试及示例可分别运行，退出码非零表示失败。

## 交换口径

进一步的字段含义、范围及兼容边界见 `docs/data-contract.md`。
