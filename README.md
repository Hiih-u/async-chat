# 🚀 Async-Chat

> **基于 FastAPI + Redis Streams + PostgreSQL 构建的企业级高并发、多模型 AI 任务编排系统**

Async-Chat 是一个生产级的异步对话系统，采用了 **Master-Detail (Batch-Task)** 架构设计。它不仅仅是一个简单的 API 包装器，而是为了解决大模型应用中的高并发、长尾延迟、故障恢复以及多模型协作（Fan-out）问题而生。

---

## 🌟 核心特性 (Key Features)

### 1. ⚡ 异步并发与扇出 (Fan-out)
* **请求扇出**：支持一次用户请求同时触发多个 AI 模型（如 Google Gemini, DeepSeek R1, Qwen）。
* **非阻塞架构**：Gateway 仅负责接收请求和派发任务，计算密集型的推理任务由后台 Workers 异步处理。
* **独立流控**：不同模型走独立的 Redis Stream 队列（`gemini_stream`, `deepseek_stream` 等），互不干扰。

### 2. 🛡️ 生产级可靠性设计
* **幂等性机制 (Idempotency)**：利用数据库原子锁 (`UPDATE ... WHERE status=PENDING`)，确保同一个任务在分布式环境下只会被执行一次，防止重复消费。
* **自动故障恢复 (Crash Recovery)**：Worker 启动时会自动扫描 Redis PEL (Pending Entries List)，自动接管并修复上一次崩溃时未完成的僵尸任务。
* **死信队列 (DLQ)**：无法解析或恶意格式的消息会自动移入 `sys_dead_letters`，防止阻塞消费组。
* **Fail-Fast 模式**：Worker 内置超时熔断与连接重试机制。

### 3. 🧠 智能路由与上下文
* **会话粘性 (Session Stickiness)**：(针对 Gemini) 优先将同一会话路由到同一后端节点，最大限度利用缓存。
* **上下文重组**：支持从数据库自动重组对话历史，实现跨 Worker 的无状态会话保持。
* 内置基于数据库 (`gemini_service_nodes`) 的轻量级服务发现与心跳检测机制，无需部署额外的注册中心。


### 4. 💎 多模型深度适配
* **Gemini Worker**：支持双路并发、软拒绝检测（自动拦截 "I cannot create images" 等拒答）。
* **DeepSeek Worker**：专为推理模型（如 DeepSeek R1）优化，支持长超时设置（300s+）以适应思维链（CoT）计算。
* **多模态支持**：支持图片/文件上传，并在 Gateway 与 Worker 间自动流转文件。



## 🏗️ 系统架构

```text
User Request
     │
     ▼
[ API Gateway (FastAPI) ] ───┬───> [ PostgreSQL (Meta/State) ]
     │ (Dispatch)            │
     ▼                       │
[ Redis Streams (MQ) ]       │ (Files)
     │                       │
     ├──> [ Gemini Worker ] ─┤
     ├──> [ DeepSeek Worker] ┤
     └──> [ Qwen Worker   ] ─┘

```

---

## 📂 项目结构

```text
async-chat/
├── api-gateway/            # 核心网关
│   ├── core/               # 路由、文件处理、节点管理逻辑
│   ├── server.py           # FastAPI 入口
│   └── static/             # 前端 UI (Web Chat)
├── workers/                # 消费者服务
│   ├── core/               # 共享内核 (消息解析、幂等锁、上下文加载)
│   ├── gemini/             # Google Gemini 专用 Worker
│   ├── deepseek/           # DeepSeek R1 专用 Worker
│   └── qwen/               # 通用 Ollama/Qwen Worker
├── common/                 # 公共模块 (数据库连接、ORM模型、日志)
├── init/                   # 数据库初始化脚本
├── docker-compose.yml      # 容器编排配置
└── requirements.txt        # 依赖列表

```

---

## 🛠️ 快速部署

### 前置要求

* **Docker & Docker Compose** (推荐)
* 或者本地安装：Python 3.10+, PostgreSQL 14+, Redis 7.x

### 1. 克隆项目与配置

```bash
git clone [https://github.com/your-repo/async-chat.git](https://github.com/your-repo/async-chat.git)
cd async-chat

python -m venv venv
source venv/bin/activate
# Windows: venv\Scripts\activate

pip install -r requirements.txt
# 复制环境变量配置文件
cp .env.example .env

```

**编辑 `.env` 文件** (关键配置):

```ini
# 数据库配置
DB_HOST=postgres
POSTGRES_PASSWORD=your_password

# Redis 配置
REDIS_HOST=redis

# 模型服务地址 (对应 Worker 的下游 API)
DEEPSEEK_SERVICE_URL=[http://host.docker.internal:11434/v1/chat/completions](http://host.docker.internal:11434/v1/chat/completions)
LLM_SERVICE_URL=[http://host.docker.internal:11434/v1/chat/completions](http://host.docker.internal:11434/v1/chat/completions)

# 日志开关
ENABLE_DB_LOG=False

```

### 2. 启动服务 (Docker Compose)

```bash
# 构建并启动所有服务
docker-compose up -d --build

```

---

## 🔌 API 接口使用

项目内置了 Swagger UI，启动后访问：`http://localhost:8000/docs`

### 1. 提交对话任务 (支持多模型)

* **Endpoint**: `POST /v1/chat/completions`
* **Content-Type**: `multipart/form-data`

| 参数 | 类型 | 说明 |
| --- | --- | --- |
| `prompt` | string | 用户提问内容 |
| `model` | string | 模型列表，逗号分隔，例如 `"gemini-2.5-flash, deepseek-r1:1.5b"` |
| `files` | file | (可选) 上传图片或文件 |
| `gemini_concurrency` | int | (可选) Gemini 并发节点数 |

**响应示例**:

```json
{
  "batch_id": "batch-uuid-1234",
  "conversation_id": "conv-uuid-5678",
  "message": "Tasks dispatched successfully",
  "task_ids": ["task-1", "task-2"]
}

```

### 2. 轮询结果 (Polling)

* **Endpoint**: `GET /v1/batches/{batch_id}`

前端通过轮询此接口获取任务进度。

### 3. 获取历史记录

* **Endpoint**: `GET /v1/conversations/{conversation_id}/history`

---

## 💻 本地开发指南

如果你不想使用 Docker，可以在本地运行：

1. **创建虚拟环境**:
```bash
python -m venv venv
source venv/bin/activate  # Windows: venv\Scripts\activate
pip install -r requirements.txt

```


2. **启动基础设施**:
确保本地已启动 PostgreSQL (端口 5432) 和 Redis (端口 6379)。
3. **运行服务**:
```bash
# 终端 1: 启动网关
python services/gateway/server.py

# 终端 2: 启动 Gemini Worker
python services/workers/gemini/gemini_worker.py

# 终端 3: 启动 DeepSeek Worker
python services/workers/deepseek/deepseek_worker.py

```


4. **访问前端**:
打开浏览器访问 `http://localhost:8000/` 即可使用内置的 Chat UI。

---

## ❓ 常见问题 (FAQ)

**Q: 为什么 DeepSeek R1 响应很慢？**
A: R1 是推理模型，会进行思维链（CoT）计算。我们在 `deepseek_worker.py` 中默认设置了 300秒 的超时时间，请耐心等待。

**Q: 如何新增一个模型 Worker？**
A: 复制 `services/workers/qwen` 目录，修改 `GROUP_NAME` 和 `STREAM_KEY`，并在 `services/gateway/core/dispatch.py` 中添加对应的路由规则即可。

**Q: 任务一直处于 PROCESSING 状态怎么办？**
A: 检查对应的 Worker 是否崩溃。重启 Worker 后，它会自动触发 `recover_pending_tasks` 流程，将僵尸任务重置并重新执行。

---

## 📄 License

MIT License

```

```