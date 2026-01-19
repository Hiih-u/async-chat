# 🚀 AI Task System (Async + Redis Streams)

这是一个基于 FastAPI 和 Redis Streams 构建的高性能异步 AI 任务处理系统。它旨在将前端的同步等待解耦，支持高并发的任务分发，并通过持久化存储管理会话历史与任务状态。

## ✨ 核心特性

* **⚡ 异步流式架构**：采用 **Redis Streams** 替代传统的 List 队列，支持消费者组（Consumer Groups）模式，确保消息不丢失，并具备 **ACK 确认机制**与**崩溃恢复（Crash Recovery）**能力。
* **🔄 统一接口路由**：通过 `/v1/chat/completions` 统一入口，根据模型名称（如 `gemini-pro`, `stable-diffusion`）自动将任务路由到对应的 Redis Stream (`gemini_stream`, `sd_stream`, etc.)。
* **💬 会话上下文管理**：内置 `Conversation` 模型，支持基于 `conversation_id` 的多轮对话历史记录查询与自动更新活跃时间。
* **🛡️ 健壮的错误追踪**：集成了 `SystemLog` 机制，自动将 Worker 或 API 的异常堆栈（Stack Trace）记录到数据库，便于排查生产环境问题。
* **🐳 容器化支持**：API Gateway 提供 Dockerfile，支持轻量级部署。

## 📂 项目结构

```text
ai-task-system/
├── api-gateway/
│   ├── Dockerfile           # API 服务容器构建文件
│   ├── server.py            # FastAPI 入口：接收请求 -> 推送 Redis Stream
│   └── main.py              # (可选入口)
├── init/
│   └── init_db.py           # 数据库初始化脚本（自动建表）
├── shared/                  # 核心共享模块
│   ├── database.py          # PostgreSQL 连接池与 Session 管理
│   ├── models.py            # SQLAlchemy ORM 模型 (Task, Conversation, SystemLog)
│   ├── schemas.py           # Pydantic 响应/请求模型
│   └── utils.py             # 通用工具 (如 log_error 数据库日志记录)
├── workers/
│   └── gemini/
│       └── http_worker.py   # 真实 Worker：监听 Stream -> 调用下游 AI 服务 -> 写回 DB
├── .env                     # 环境变量配置
├── .gitignore               # Git 忽略配置
├── requirements.txt         # 项目依赖
└── README.md                # 项目文档

```

## 🛠️ 技术栈

* **Web 框架**: FastAPI (Python 3.10+)
* **数据库**: PostgreSQL (SQLAlchemy ORM + Psycopg2)
* **消息中间件**: Redis (Streams + Consumer Groups)
* **容器化**: Docker
* **依赖管理**: Pydantic v2, Uvicorn

## ⚙️ 快速开始

### 1. 环境准备

确保本地或服务器已安装 PostgreSQL 和 Redis。

```bash
# 1. 克隆项目
git clone <your-repo-url>
cd ai-task-system

# 2. 创建并激活虚拟环境
python -m venv venv
source venv/bin/activate  # Linux/Mac
# venv\Scripts\activate   # Windows

# 3. 安装依赖
pip install -r requirements.txt

```

### 2. 配置文件 (.env)

在项目根目录创建 `.env` 文件，填入你的配置信息：

```ini
# --- 数据库配置 (PostgreSQL) ---
DB_HOST=127.0.0.1
DB_PORT=5432
POSTGRES_DB=gemini
POSTGRES_USER=postgres
POSTGRES_PASSWORD=your_password

# --- Redis 配置 ---
REDIS_HOST=127.0.0.1
REDIS_PORT=6379

# --- Worker 配置 ---
# 下游真实 AI 服务的地址 (Worker 会将请求转发到这里)
GEMINI_SERVICE_URL=http://localhost:61080/v1/chat/completions
# Worker 身份标识 (用于 Redis Consumer Group 区分消费者)
WORKER_ID=gemini-worker-01

```

### 3. 初始化数据库

首次运行前，执行初始化脚本创建表结构 (`ai_tasks`, `ai_conversations`, `sys_logs`)：

```bash
python init/init_db.py

```

*输出 `✅ 数据库表结构同步完成！` 即表示成功。*

## 🚀 启动服务

建议开启两个终端窗口，分别运行 API 服务和 Worker。

### 终端 1：启动 API Gateway

```bash
# 开发模式
uvicorn api-gateway.server:app --reload --host 0.0.0.0 --port 8000

# 或直接运行脚本
python api-gateway/server.py

```

### 终端 2：启动 Gemini Worker

该 Worker 会加入 Redis 的消费者组，监听任务并调用配置的 `GEMINI_SERVICE_URL`。

```bash
python workers/gemini/http_worker.py

```

## 🔌 API 接口说明

访问 Swagger 文档：`http://127.0.0.1:8000/docs`

### 1. 统一对话/任务入口

支持文本对话或触发其他模态任务，系统根据 `model` 字段自动分发。

* **URL**: `POST /v1/chat/completions`
* **Request Body**:
```json
{
  "prompt": "如何用 Python 读取 CSV 文件？",
  "model": "gemini-2.5-flash", 
  "conversation_id": null 
}

```


*说明：如果不传 `conversation_id`，系统会新建会话并返回 ID；如果传入模型名包含 `sd` 或 `stable`，任务会被分发到 `sd_stream` 队列。*
* **Response**:
```json
{
  "task_id": "uuid-string...",
  "status": 0,
  "conversation_id": "uuid-string...",
  "message": "对话请求已入队"
}

```



### 2. 查询任务状态 (轮询)

前端拿到 `task_id` 后轮询此接口，直到 `status` 变为 1 (SUCCESS) 或 2 (FAILED)。

* **URL**: `GET /v1/tasks/{task_id}`
* **Response**:
```json
{
  "task_id": "...",
  "status": 1,
  "response_text": "这里是 AI 的回复内容...",
  "cost_time": 2.5,
  "created_at": "2024-01-01T12:00:00"
}

```



### 3. 获取会话历史

获取某个会话的完整上下文，用于前端展示历史聊天记录。

* **URL**: `GET /v1/conversations/{conversation_id}/history`

## 🧠 核心机制：Redis Stream 流程

1. **生产 (API)**: 用户请求到达 API Gateway -> `server.py` 解析模型名 -> `XADD` 写入对应的 Stream (如 `gemini_stream`)。
2. **消费 (Worker)**: `http_worker.py` 作为消费者组 (`gemini_workers_group`) 成员，通过 `XREADGROUP` 获取待处理消息。
3. **处理**: Worker 解析 payload -> 请求下游 `GEMINI_SERVICE_URL` -> 获取结果。
4. **持久化 & ACK**:
* **成功**: 将结果更新至 PostgreSQL `ai_tasks` 表，状态置为 `SUCCESS`，并执行 `XACK` 确认消息已消费。
* **失败**: 记录错误信息至 `sys_logs` 和 `ai_tasks`，视错误类型决定是否 ACK (避免死循环)。


5. **崩溃恢复**: Worker 启动时会自动检查 Pending List (PEL)，重新处理那些“已认领但未确认（Worker 意外崩溃）”的旧任务。