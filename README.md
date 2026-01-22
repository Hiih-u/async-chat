# 🚀 AI Task System (Async + Redis Streams)

这是一个基于 **FastAPI** 和 **Redis Streams** 构建的企业级异步 AI 任务处理系统。它将前端的同步等待解耦，支持高并发的任务分发，并通过 Consumer Group 模式实现可靠的消息处理与故障恢复。

## 🌟 核心特性

* **⚡ 异步流式架构**：采用 **Redis Streams** 替代传统 List 队列，支持多消费者组（Consumer Groups），确保高吞吐量与消息不丢失。
* **🔄 智能模型路由**：
* `api-gateway` 根据请求中的 `model` 字段自动分发任务到不同的 Stream：
* `gemini-*` → **`gemini_stream`**
* `deepseek-*` → **`deepseek_stream`** (New!)
* `sd-*` / `stable-*` → **`sd_stream`**




* **🛡️ 健壮的可靠性设计**：
* **崩溃恢复 (Crash Recovery)**：Worker 启动及心跳检测时，会自动扫描 PEL (Pending Entries List)，重新处理那些“已认领但未确认”的旧任务。
* **幂等性检查 (Idempotency)**：在处理消息前会检查数据库状态，防止因重复消费导致的任务重复执行。


* **📊 完整的日志与追踪**：
* **SystemLog**：自动捕获异常堆栈 (Stack Trace) 并持久化到 PostgreSQL，支持通过 `.env` 开关控制。
* **Conversation Context**：内置会话管理，支持存储和查询多轮对话历史。



## 📂 项目结构

```text
ai-task-system/
├── api-gateway/
│   ├── Dockerfile           # API 服务容器构建
│   ├── server.py            # FastAPI 入口：任务分发与路由逻辑
│   └── main.py              # (可选入口)
├── workers/
│   └── gemini/
│       └── http_worker.py   # 核心 Worker：消费 Stream -> 幂等检查 -> 调用 AI -> 落库 -> ACK
├── shared/                  # 共享模块
│   ├── database.py          # 数据库连接池与 Session 管理
│   ├── models.py            # ORM 模型: Task, Conversation, SystemLog
│   ├── schemas.py           # Pydantic 数据验证模型
│   └── utils.py             # 通用工具 (含数据库日志记录器)
├── init/
│   └── init_db.py           # ⚠️ 数据库初始化脚本 (自动建表)
├── .env                     # 环境变量配置
├── requirements.txt         # 项目依赖
└── README.md                # 项目文档

```

## 🛠️ 技术栈

* **Web 框架**: FastAPI (Python 3.10+)
* **数据库**: PostgreSQL (SQLAlchemy ORM + Psycopg2)
* **消息队列**: Redis Streams (5.0+)
* **依赖管理**: Pydantic v2, Uvicorn, Requests

## ⚙️ 快速开始

### 1. 环境准备

确保本地已安装 PostgreSQL 和 Redis。

```bash
# 1. 克隆项目 & 进入目录
git clone https://github.com/Hiih-u/AI-task-system.git
cd ai-task-system

# 2. 创建并激活虚拟环境
python -m venv venv
source venv/bin/activate  # Linux/Mac
# venv\Scripts\activate   # Windows

# 3. 安装依赖
pip install -r requirements.txt

```

### 2. 配置文件 (.env)

在项目根目录创建 `.env` 文件，根据实际环境修改配置。

```ini
# --- 数据库配置 ---
DB_HOST=127.0.0.1
DB_PORT=5432
POSTGRES_DB=gemini
POSTGRES_USER=postgres
POSTGRES_PASSWORD=your_password

# --- Redis 配置 ---
REDIS_HOST=127.0.0.1
REDIS_PORT=6379

# --- Worker 配置 ---
# 下游真实 AI 服务地址 (Worker 会透传请求)
GEMINI_SERVICE_URL=http://localhost:61080/v1/chat/completions

# Worker 身份标识 (用于 Redis Consumer Group 区分消费者)
WORKER_ID=gemini-worker-01

# --- 日志配置 ---
# True: 开启数据库错误日志记录 (开发调试推荐)
# False: 仅控制台输出 (生产环境推荐，减少数据库压力)
ENABLE_DB_LOG=True

```

### 3. 初始化数据库

⚠️ **警告**：此脚本会执行 `DROP ALL`，清空现有数据库表结构和数据！仅在首次部署或重置环境时使用。

```bash
python init/init_db.py

```

*输出 `✅ 数据库表结构同步完成！` 即表示成功。*

## 🚀 启动服务

建议开启两个终端窗口分别运行组件。

### 终端 1：启动 API Gateway

API 服务默认运行在 8000 端口，提供 Swagger 文档。

```bash
# 开发模式 (支持热重载)
uvicorn api-gateway.server:app --reload --host 0.0.0.0 --port 8000

```

### 终端 2：启动 Worker

Worker 启动后会自动加入 `gemini_workers_group` 消费者组，并开始处理任务。

```bash
python workers/gemini/gemini_worker.py

```

*启动成功后你将看到 `🚀 Stream Worker 启动` 以及 `♻️ 发现 x 个挂起任务` 的日志。*

## 🔌 API 接口说明

完整文档请访问 Swagger UI: `http://127.0.0.1:8000/docs`

### 1. 提交对话任务

* **URL**: `POST /v1/chat/completions`
* **Body**:
```json
{
  "prompt": "如何用 Python 读取 CSV？",
  "model": "gemini-2.5-flash", 
  "conversation_id": null 
}

```


> **Tip**: 如果 `model` 包含 "deepseek"，任务会自动路由到 `deepseek_stream`；如果包含 "sd" 或 "stable"，则路由到 `sd_stream`。


* **Response**:
```json
{
  "task_id": "550e8400-e29b...",
  "status": 0,
  "conversation_id": "c123...",
  "message": "对话请求已入队"
}

```



### 2. 查询任务结果 (轮询)

前端拿到 `task_id` 后轮询此接口，直到 `status` 变为 `1` (SUCCESS) 或 `2` (FAILED)。

* **URL**: `GET /v1/tasks/{task_id}`
* **Response**:
```json
{
  "task_id": "...",
  "status": 1,
  "task_type": "TEXT",
  "response_text": "可以使用 pandas 库...",
  "cost_time": 1.25,
  "created_at": "2024-03-20T10:00:00"
}

```



### 3. 获取会话历史

获取某个会话的完整上下文，用于前端展示历史聊天记录。

* **URL**: `GET /v1/conversations/{conversation_id}/history`

## 🧠 核心机制详解

### 1. 消息生命周期

1. **API** 接收请求 -> 解析 Model -> `XADD` 写入对应 Stream (maxlen~10)。
2. **Worker** 通过 `XREADGROUP` 抢占消息 (进入 Pending 状态)。
3. **Worker** 处理业务 -> **更新 DB** -> **XACK** (从 Pending 中移除)。

### 2. 崩溃恢复流程 (Crash Recovery)

如果 Worker 在 `XACK` 之前崩溃：

* 消息会一直停留在 Redis 的 **PEL (Pending Entries List)** 中。
* Worker 重启时，或心跳检测 (`recover_pending_tasks`) 触发时，会扫描这些旧消息。
* **关键机制**：在重试前，Worker 会先查数据库 (`check_idempotency=True`)。如果发现该任务 ID 已经是 `SUCCESS`，则直接补发 `XACK` 而不重复执行 AI 请求。

## ❓ 常见问题

**Q: 为什么 Redis Stream 里的消息数量有时候会超过 10 条？**
A: 代码中使用了 `maxlen=10`，Redis 默认采用近似修剪 (`~`) 算法。这是为了性能优化，允许流长度在小范围内（如 10-20 条）浮动，属于正常现象。

**Q: 如何横向扩展 Worker？**
A: 可以在多台机器上启动 `http_worker.py`，只需修改 `.env` 中的 `WORKER_ID` (如 `gemini-worker-02`)，它们会自动加入同一个消费者组，实现负载均衡。