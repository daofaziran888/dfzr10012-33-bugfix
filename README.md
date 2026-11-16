# 实时流数据处理中心

面向实时数据处理团队的流式计算与管理中心。覆盖流数据源接入、流处理算子编排、窗口计算配置、状态后端管理、检查点恢复、反压监控、延迟追踪与流作业运维的完整实时计算链路。

这是一个前后端分离的管理平台：前端 React + Vite + TypeScript，后端 FastAPI（Python）。
两边各自独立启动，前端 dev server 已关掉自动打开页面，启动后按终端打印的地址手工打开。

## 目录结构

```text
.
├── frontend/                 React + Vite + TypeScript 前端
│   ├── src/views/            每个业务模块一个页面
│   ├── src/api/              统一请求封装
│   ├── src/stores/           会话与筛选状态
│   └── vite.config.ts        dev server 配置（open: false）
├── backend/                  FastAPI（Python） 后端
│   ├── app/routers/          每个业务模块一组接口
│   ├── app/services/         业务规则与状态流转
│   └── app/store.py          内存数据仓库与示例数据
├── .gitignore
└── docker-compose.yml
```

## 启动

### 后端

```bash
cd backend
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
./run.sh
```

健康检查：`curl http://127.0.0.1:8000/api/health`

### 前端

```bash
cd frontend
npm install
npm run dev
```

前端默认监听 `http://127.0.0.1:5173/`，dev server 不会自动打开浏览器，
需要自己访问。`/api` 由 vite 代理到后端 `http://127.0.0.1:8000`。

## 业务模块

| 模块 | 目录 | 业务对象 | 主要字段 |
| --- | --- | --- | --- |
| 流数据源接入 | `streamin` | 流数据源 | 源编号、源名称、消息队列 |
| 流处理算子 | `operator` | 算子定义 | 算子编号、算子名称、输入流 |
| 窗口计算配置 | `window` | 窗口定义 | 窗口编号、数据流名称、窗口类型 |
| 状态后端管理 | `state` | 后端配置 | 配置编号、后端类型、存储路径 |
| 检查点恢复 | `checkpoint` | 检查点记录 | 记录编号、流作业名、检查点ID |
| 反压监控面板 | `backpress` | 反压指标 | 指标编号、流作业名、反压级别 |
| 延迟追踪管理 | `latency` | 延迟记录 | 记录编号、流作业名、端到端延迟 |
| 流作业运维 | `jobops` | 作业实例 | 实例编号、作业名称、运行节点 |
| 流数据输出端 | `sink` | 输出配置 | 配置编号、输出类型、目标地址 |
| 流数据格式定义 | `schema` | 格式定义 | 定义编号、流名称、字段名称 |
| 水位线管理 | `watermark` | 水位线配置 | 配置编号、流名称、时间字段 |
| 并行度调优 | `parallel` | 调优记录 | 记录编号、流算子名、当前并行度 |
| 流作业指标采集 | `metric` | 指标采集器 | 采集器编号、作业名称、指标名称 |
| 流告警规则 | `alarm` | 告警规则 | 规则编号、监控指标、告警条件 |
| 自定义函数库 | `udf` | 函数定义 | 函数编号、函数名称、输入参数 |
| 流处理日志中心 | `log` | 日志记录 | 记录编号、流作业名、日志级别 |

## 约定

- 每个模块的前端页面在 `frontend/src/views/<模块>/index.vue`，后端接口在
  `backend/app/routers/<模块>.py`，业务规则在 `backend/app/services/<模块>.py`。
- 列表接口统一返回 `{ items, total, page, size }`，动作接口统一返回 `{ ok, message }`。
- 状态流转只允许在 `app/services` 里改，路由层不做业务判断。

## 自定义函数引用

自定义函数库与流处理算子之间靠「函数引用数据结果」串联，这份派生数据存在内部表
`udf_ref`（不参与运营概览统计）：

- 算子上的 `引用函数` 声明引用了哪些函数编号；函数侧的 `输入参数`、`返回类型`、
  `函数代码` 是引用校验必须读到的字段。
- `停用函数` 与 `校验引用` 会先做引用索引匹配，再把匹配到的 `匹配算子`、`停用状态`、
  `测试用例` 写成一条结果记录；记录按 `版本` 追加，历史引用保持可追溯。
- 算子 `发布算子` 只消费最新且已确认的结果记录，并按 `记录编号` 关联；
  命中已停用函数会直接拒绝发布，不会把已停用函数静默带入新的执行链路。
- 写入走 `app/store.py` 的 `transaction()`：任何一步失败都会把函数字段、引用结果和
  算子引用一起退回；调用方可用 `期望版本` 做乐观并发控制，竞态下只接受一方。

相关接口：

| 接口 | 用途 |
| --- | --- |
| `GET /api/udf/references` | 分页读取引用数据结果，可按函数编号、记录编号、停用状态过滤 |
| `GET /api/udf/references/{记录编号}` | 按记录编号读取单条结果 |
| `GET /api/udf/{id}/reference-preview` | 只读预览引用索引匹配，不写库 |
| `GET /api/udf/{id}/references` | 某个函数的全部结果（按版本升序） |
| `POST /api/udf/{id}/actions` | 动作：`注册函数`、`校验引用`、`停用函数`、`删除函数` |
| `GET /api/operator/{id}/publish-preview` | 只读预览发布校验与将要消费的记录编号 |
| `POST /api/operator/{id}/actions` | 动作：`启动算子`、`暂停算子`、`下线算子`、`发布算子` |

领域：数据工程
子方向：数仓与数据湖
