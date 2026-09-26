# 自然史标本与实验协作服务

本项目是一套可离线运行的 Python 后台，用于自然史馆、学校实验室和野外调查团队协同管理昆虫、植物及其他生物标本。系统把保藏与转运、分类实验复核、生物安全处置三个业务子域保存在 SQLite 中，提供角色权限、幂等请求、事务状态、版本化记录和可追溯审计。

## 目录

- `src/collection_logistics/`：馆藏环境指标、库房与转运路线、保藏资源、调拨任务和调整情景；
- `src/taxonomy_lab/`：采集设备、实验协议、观察记录导入、异常排除、分析租约、鉴定决定，以及标本级版本化鉴定稿（材料/分析批次/模式照片证据登记与撤回、初审—专科复核—终审顺序会签、失效保留历史）；
- `src/biosafety_ops/`：库区记录、有害生物监测、风险告警、处置工单和资源分配；
- `fixtures/`：离线验收使用的实验协议与结构化观察记录；
- `tests/`：领域规则、事务边界、权限、HTTP API 和命令行验收测试。

## 环境

- Linux
- Python 3.11 或更高版本
- 运行时只依赖 Python 标准库与 SQLite

## 测试

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -q
```

## 构建检查

```bash
python3 -m compileall -q src tests
```

## 离线验收

```bash
PYTHONPATH=src python3 -m collection_logistics.acceptance --workspace .
PYTHONPATH=src python3 -m taxonomy_lab.acceptance --workspace .
PYTHONPATH=src python3 -m biosafety_ops.acceptance
```

验收会建立临时 SQLite 数据库，登记馆藏环境指标、保藏库房、转运路线和材料批次，完成实验观察导入、异常复核、生物安全告警与资源分配，并输出 JSON 结果。命令不访问公网，也不需要额外数据库、队列或常驻服务。

## HTTP API

```bash
PYTHONPATH=src python3 -m collection_logistics.api --database collection.sqlite3 --host 127.0.0.1 --port 8080
PYTHONPATH=src python3 -m taxonomy_lab.api --database taxonomy.sqlite3 --host 127.0.0.1 --port 8081
PYTHONPATH=src python3 -m biosafety_ops.api --database biosafety.sqlite3 --host 127.0.0.1 --port 8082
```

三个服务均提供 `GET /health`，其余接口使用 JSON。SQLite 文件保存业务状态、幂等结果和审计记录，进程重启后可以继续查询与复核。

## 版本化鉴定稿（模块 006）

`taxonomy_lab` 在批次观察采信之上提供标本级正式分类结论：

- `POST /specimens`、`POST /specimens/{id}/evidence`：登记标本与证据（`material` / `analysis_batch` / `type_photo`）；`POST /evidence/{id}/withdraw` 撤回证据；
- `POST /specimens/{id}/determination_drafts`：建立版本化鉴定稿，引用具体材料与分析批次，可申报利益相关人；
- `POST /determinations/{id}/sign`：按初审、专科复核、终审顺序会签；同一签署人重复提交幂等返回，不产生第二份决定；
- `GET /specimens/{id}/determination`：科普内容使用的当前有效结论（无需角色）；无有效版本时返回最近失效版本及原因；
- `GET /specimens/{id}/determinations`、`GET /determinations/{id}`：研究人员追溯每次修订、签署人与引用依据（限复核、审批与审计角色）。

规则要点：新增材料或撤回被引用证据时，已签当前版本自动失效但历史完整保留；同一标本并行提交只能产生一个当前有效版本（部分唯一索引保证）；存在利益回避、证据缺口或学名冲突/重复决定时，终审发布被阻断并返回结构化原因（`determination_publish_blocked`）。
