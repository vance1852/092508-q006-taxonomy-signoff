# 自然史标本与实验协作服务

本项目是一套可离线运行的 Python 后台，用于自然史馆、学校实验室和野外调查团队协同管理昆虫、植物及其他生物标本。系统把保藏与转运、分类实验复核、生物安全处置三个业务子域保存在 SQLite 中，提供角色权限、幂等请求、事务状态、版本化记录和可追溯审计。

## 目录

- `src/collection_logistics/`：馆藏环境指标、库房与转运路线、保藏资源、调拨任务和调整情景；
- `src/taxonomy_lab/`：采集设备、实验协议、观察记录导入、异常排除、分析租约、鉴定决定，以及标本维度的版本化鉴定稿（证据引用、三级会签、失效留痕与学名裁定）；
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

## 版本化鉴定稿（taxonomy_lab）

针对单个标本建立可引用的正式分类结论，独立于"观察与分析结果"链路：

- 每个标本有递增的鉴定稿版本（draft → published / rejected；published 之后可被 superseded 或 invalidated）；
- 每版在建稿时**固化证据引用快照**：初鉴形态记录（initial_identification）、分子分析批次（molecular_batch，可带分析批次摘要）、模式照片（type_photograph）；
- 会签严格按 **初审 initial_review → 专科复核 specialist_review → 终审 final_review** 顺序进行，分别由三个新角色承担，`UNIQUE(version, stage)` 保证重复签署不会产生第二份决定；签署人命中利益回避登记时在签署环节直接 403 拦截；
- 终审通过时执行发布前阻断检查：三类证据缺一不可、引用证据不得已撤回、学名不得与其他标本的当前结论冲突；阻断返回 422 `publication_blocked` 并在 `error.reasons` 中列出全部原因，且整个签署回滚；
- **新增材料**或**撤回被引用证据**时，当前有效版本（及会签中草稿，撤回场景）在同一事务内置为 `invalidated` 并记录原因，旧版本的结论、引用快照与签署人全部保留、不被改写；
- 部分唯一索引保证每标本至多一个 draft、至多一个 published，配合 `BEGIN IMMEDIATE` 使并行提交只产生一个当前版本。

接口（均为 JSON，写接口需 `X-Actor-Id`）：

| 方法 & 路径 | 说明 |
| --- | --- |
| `POST /specimens` | 登记标本 |
| `POST /specimens/{id}/evidence` | 登记三类证据（content_sha256 强制 64 位） |
| `POST /specimen_evidence/{id}/withdraw` | 撤回证据并失效引用它的版本 |
| `POST /reviewers/assign` | 指派三级会签人（approver） |
| `POST /reviewer_conflicts` | 登记签署人对某标本的利益回避 |
| `POST /specimens/{id}/determinations` | 创建版本化鉴定稿（引用证据 ID 列表） |
| `POST /specimens/{id}/determinations/{vid}/sign` | 按当前用户角色对应环节会签（approve/comment） |
| `GET /specimens/{id}/determination/current` | **科普端公共只读**：当前有效结论，无则 404 |
| `GET /specimens/{id}/determinations` | **研究端**（需 determination.read）：完整修订链、每版引用与签署人 |
| `GET /specimens/{id}/determinations/{vid}` | 研究端：单个版本的完整追溯 |

