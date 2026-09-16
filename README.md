# 舞台排演提示单（Stage Cue Sheet）

舞台排演用的提示单 Web 应用：每条提示（Cue）包含**部门、名称、时长**，可依赖多条
前置提示并设置延迟，也可**锁定开场秒数**。系统按依赖图自动排程并在时间轴上可视化。

- 后端：**FastAPI + SQLite**（零外部服务，单文件数据库）
- 前端：**原生 HTML / CSS / JavaScript**（无构建步骤、无 npm 依赖）
- 部署：**Docker 一键启动**，数据存于 Docker 卷，刷新 / 重启不丢失

## 一条命令启动

```bash
docker compose up -d --build
```

启动后浏览器访问：

> **http://localhost:8000**

首次启动会自动建表并写入一组演示数据（含一条锁定冲突链演示）。
停止 / 查看日志：

```bash
docker compose down        # 停止（保留数据）
docker compose logs -f     # 查看日志
```

> 需要完全重置数据：`docker compose down -v`（删除数据卷）后重新 `up`。

## 环境变量与配置

所有变量均有默认值，**不设置任何变量也能直接启动**。可在项目根目录创建 `.env`
（参考 `.env.example`），或导出为环境变量后再执行 `docker compose up`：

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `HOST_PORT` | `8000` | 宿主机映射端口。端口被占用时改为其他值，如 `HOST_PORT=8080` |
| `SEED_DEMO` | `1` | 首次启动且数据库为空时是否写入演示数据，`1`=开启 / `0`=关闭 |
| `CUE_DB_PATH` | `/data/cues.db` | 容器内 SQLite 文件路径（位于数据卷中，一般无需修改） |

示例（改用 8080 端口、空库启动）：

```bash
HOST_PORT=8080 SEED_DEMO=0 docker compose up -d --build
# 访问 http://localhost:8080
```

## 功能说明

- **提示管理**：新增、编辑、删除提示；删除时自动清理它与其他提示之间的依赖关系。
- **依赖与延迟**：一条提示可设置多条前置；未锁定时
  `开始时间 = max(各前置结束时间 + 对应延迟)`，无前置则从 0 秒开始。
- **锁定开场**：可为提示指定固定开场秒数。
- **拓扑级联重算**：任何修改后，所有提示按拓扑顺序重新计算开始 / 结束时间。
- **成环拒绝**：新增 / 修改依赖若导致依赖图出现环，返回 `409` 并整体回滚，
  界面提示成环的提示编号，绝不写入脏数据。
- **锁定冲突**：锁定时间早于依赖链允许的最早开始时间时，**保留锁定值**，
  同时在页面顶部与时间轴上标出**完整冲突链**（从根提示沿关键依赖路径到冲突提示，
  冲突节点红色高亮、冲突依赖边红色曲线）。
- **排演执行台**：一键开始排演，把当前**提示单、依赖关系与计划时间**冻结为本场快照，
  之后编辑 / 删除提示单均不影响进行中的场次；同一时刻只允许一场进行中的排演
  （重复开始返回 `409`）。
  - 执行状态：`等待 → 就绪 → 执行中 → 已完成`。所有前置均已完成，且场钟达到
    `max(各前置实际完成时间 + 对应延迟, 锁定开场时间)` 时提示进入**就绪**；
    无前置且未锁定的提示立即就绪。
  - 「开始 / 完成」由后端记录**相对本场起点的秒数**；非法状态跳转返回 `409`。
  - 页面显示实时场钟、计划与实际时间及偏差（正偏差红色 = 延误），
    支持结束本场；刷新页面或重启容器后继续显示同一场排演及已记录状态。
- **部门筛选**：顶部下拉按部门筛选时间轴与列表（筛选选择会保存在浏览器本地）。
- **时间轴缩放**：缩放滑块 / `＋` `−` 按钮调整每秒像素数，「适配」自动缩放到全局，
  缩放比例本地持久化。
- **数据持久化**：SQLite 文件存于 Docker 卷 `cue-data`，页面刷新、容器重启数据不丢。

## HTTP API

服务启动后可访问交互式文档：**http://localhost:8000/docs**

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `GET` | `/api/health` | 健康检查 |
| `GET` | `/api/schedule` | 完整排程结果：提示（含 start/end/冲突标记/冲突链）、依赖边、冲突列表、环信息 |
| `GET` | `/api/cues/{id}` | 单条提示详情（含前置与延迟） |
| `POST` | `/api/cues` | 新增提示（含前置依赖），成环返回 `409` |
| `PUT` | `/api/cues/{id}` | 全量更新提示（含前置依赖），成环返回 `409` |
| `DELETE` | `/api/cues/{id}` | 删除提示并级联清理依赖 |
| `POST` | `/api/rehearsals` | 开始排演并冻结快照（已有进行中场次返回 `409`），Body 可选 `{ "name": "…" }` |
| `GET` | `/api/rehearsals/current` | 当前排演及实时执行状态（进行中优先，否则最近一场；无排演时 `rehearsal` 为 `null`） |
| `POST` | `/api/rehearsals/{id}/cues/{cue_id}/start` | 开始执行某提示，记录相对本场起点的秒数（未就绪 / 状态非法返回 `409`） |
| `POST` | `/api/rehearsals/{id}/cues/{cue_id}/complete` | 完成某提示，记录相对本场起点的秒数（非执行中返回 `409`） |
| `POST` | `/api/rehearsals/{id}/end` | 结束本场排演（已结束返回 `409`） |

请求体示例：

```json
{
  "department": "灯光",
  "name": "第一幕灯光",
  "duration": 120,
  "locked_start": null,
  "sort_order": 0,
  "predecessors": [
    { "id": 4, "delay": 0 }
  ]
}
```

`GET /api/schedule` 中每个提示带 `start` / `end` / `allowed` / `conflict` /
`in_conflict_chain` / `conflict_path` 等字段；`conflicts[].path` 即完整冲突链
（提示 id 数组，根在前），`conflicts[].chain_edges` 为链上的依赖边 `[from, to]`。

`GET /api/rehearsals/current` 返回场次信息（`status` / `start_epoch` / `elapsed` /
`server_now`）与快照提示列表；每条提示含 `status`（`waiting` / `ready` / `executing` /
`completed`）、`planned_start` / `planned_end`、`actual_start` / `actual_end`、
`start_dev` / `end_dev`（实际 − 计划偏差秒数）与 `ready_at`（预计就绪时刻）。

## 本地开发（不使用 Docker）

需要 Python 3.11+：

```bash
pip install -r requirements.txt
CUE_DB_PATH=./cues.db SEED_DEMO=1 uvicorn app.main:app --reload --port 8000
# http://localhost:8000
```

## 项目结构

```
.
├── app/
│   ├── main.py        # FastAPI 路由、启动初始化（建表/演示数据）
│   ├── scheduler.py   # 拓扑排序、级联重算、环检测、冲突链提取（纯标准库）
│   ├── rehearsal.py   # 排演执行台：快照冻结、就绪门限、执行状态机
│   ├── database.py    # SQLite 连接、表结构、演示数据
│   └── schemas.py     # Pydantic 校验模型
├── static/
│   ├── index.html     # 单页界面
│   ├── style.css
│   ├── app.js         # 时间轴渲染 / 缩放 / 筛选 / 编辑
│   └── rehearsal.js   # 排演执行台：实时场钟 / 状态轮询 / 开始·完成·结束
├── Dockerfile
├── compose.yaml
├── .env.example
└── requirements.txt
```
