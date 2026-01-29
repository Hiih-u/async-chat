# 🚀 Async-Chat

> **基于 FastAPI + Redis Streams + PostgreSQL 构建的高并发、多模型 AI 任务编排系统**

本项目不仅仅是一个简单的 Chat API，而是一个生产级的异步任务处理架构。它采用了 **Master-Detail (批次-任务)** 设计模式，支持 **请求扇出 (Request Fan-out)**，即一次用户请求可以同时触发多个 AI 模型（如 Google Gemini, DeepSeek R1, Qwen）并行处理，并具备完善的故障恢复与幂等性机制。

---

## 🌟 核心亮点 (Key Features)

### 1. ⚡ 多模型并发扇出 (Multi-Model Fan-out)

打破“一次请求对应一个模型”的限制。

* **并发执行**：用户只需发送一次请求，网关自动创建 `ChatBatch`，并将其拆分为多个独立的 `Task` 派发给不同的 Worker。
* **混合编排**：支持异构模型同时工作，例如让 **Gemini 2.5** 负责逻辑推理，同时让 **DeepSeek R1** 进行深度思考。
* **独立流控**：每个模型的任务走独立的 Redis Stream 队列，互不阻塞。

### 2. 💎 增强型 Gemini Worker

专门为 Google Gemini 业务场景深度定制的 Worker (`workers/gemini/gemini_worker.py`)：

* **🍬 会话粘性 (Session Stickiness)**：优先将同一会话路由到同一后端节点，最大限度利用缓存。如果节点变更，自动从数据库重组完整上下文 (`Context Reconstruction`)。
* **🛡️ 软拒绝检测 (Soft Refusal Check)**：内置内容审查机制，自动拦截如 "I cannot create images" 等拒答回复，并标记任务状态，防止无效内容污染上下文。

### 3. 🛡️ 生产级可靠性

* **幂等性设计 (Idempotency)**：通过数据库原子锁 (`UPDATE ... WHERE status=PENDING`) 防止多 Worker 抢占同一任务。
* **崩溃恢复 (Crash Recovery)**：Worker 启动时自动扫描 Redis PEL (Pending Entries List)，接管并修复上一次崩溃时未完成的任务。
* **死信队列 (DLQ)**：无法解析或恶意格式的消息自动移入死信队列，防止阻塞消费组。
* **全链路追踪**：从 `Batch` 到 `Task` 再到 `SystemLog`，完整记录任务生命周期与错误堆栈。

---

## 📂 项目结构

```text
ai-task-system/
├── api-gateway/
│   ├── server.py            # 核心网关：负责 Batch 创建、任务拆分与 Redis 路由
│   └── static/              # 前端 UI (支持 Markdown 渲染与实时轮询)
├── workers/
│   ├── gemini/              # ✨ [核心] Gemini 专用 Worker (含 Nacos/粘性会话)
│   ├── deepseek/            # DeepSeek R1 专用 Worker
│   └── qwen/                # 通用 Ollama/Qwen Worker
├── shared/                  # 共享内核 (核心库)
│   ├── core/                # 核心逻辑 (路由、消息解析、状态机、审计)
│   ├── database.py          # 数据库连接池 (Pool Pre-Ping)
│   └── models.py            # SQLAlchemy 模型
├── init/                    # 数据库初始化脚本
├── docker-compose.yml       # 容器编排
└── requirements.txt         # 依赖列表

```

---

## 🛠️ 快速部署

### 1. 环境准备

确保本地或服务器已安装：

* **Python 3.10+**
* **Redis 7.x**
* **PostgreSQL 14+**
* (可选) **Nacos** (仅 Gemini Worker 需要)

### 2. 安装与配置

```bash
git clone https://github.com/your-repo/async-chat.git
cd async-chat

python -m venv venv
source venv/bin/activate
# Windows: venv\Scripts\activate

pip install -r requirements.txt

cp .env.example .env
# 编辑 .env 文件，配置 DB_HOST, REDIS_HOST 以及 Nacos 地址

```

### 3. 初始化数据库

```bash
python init/init_db.py
# 输出 ✅ 数据库表结构同步完成！ 即为成功

```

### 4. 启动服务

**方式 A: Docker Compose (推荐)**

```bash
docker-compose up -d --build

```

**方式 B: 手动启动**

```bash
# 终端 1: 启动 API 网关
python gateway/server.py

# 终端 2: 启动 Gemini Worker
python workers/gemini/gemini_worker.py

# 终端 3: 启动 DeepSeek Worker (可选)
python workers/deepseek/deepseek_worker.py

```

---

## 🔌 API 接口使用

### 1. 提交并发任务 (Fan-out)

一次调用，触发多个模型并行生成。

* **Endpoint**: `POST /v1/chat/completions`
* **Payload**:

```json
{
  "prompt": "请分析 Python 的 GIL 锁机制",
  "model": "gemini-2.5-flash, deepseek-r1:1.5b, qwen2.5:7b",
  "conversation_id": null
}

```

> **注意**: `model` 字段使用逗号分隔。网关会自动拆分为 3 个独立的 Task。

* **Response**:

```json
{
  "batch_id": "batch-uuid-...",
  "conversation_id": "conv-uuid-...",
  "message": "Tasks dispatched successfully",
  "task_ids": ["task-1...", "task-2...", "task-3..."]
}

```

### 2. 轮询结果 (Polling)

前端通过此接口轮询，直到所有模型都返回结果。

* **Endpoint**: `GET /v1/batches/{batch_id}`
* **Response**:

```json
{
  "batch_id": "...",
  "status": "PROCESSING",
  "results": [
    {
      "model_name": "gemini-2.5-flash",
      "status": 1, 
      "response_text": "GIL (Global Interpreter Lock)...",
      "cost_time": 1.2
    },
    {
      "model_name": "deepseek-r1:1.5b",
      "status": 3,
      "response_text": null
    }
  ]
}

```

---

## 🔧 高级配置 (Env Variables)

| 变量名 | 默认值 | 说明 |
| --- | --- | --- |
| `GEMINI_WORKER_ID` | random | Gemini Worker 的唯一标识，用于日志追踪 |
| `NACOS_SERVER_ADDR` | 127.0.0.1:8848 | Nacos 服务地址，用于 Gemini 服务发现 |
| `ENABLE_DB_LOG` | True | 是否将错误堆栈写入 `sys_logs` 表 (生产建议 False) |
| `DEEPSEEK_SERVICE_URL` | localhost:11434 | DeepSeek/Ollama 的 API 地址 |
| `STREAM_KEY` | gemini_stream | Redis Stream 队列名称 |

---

## ❓ 常见问题

**Q: 如何处理 "Gemini Worker 无法连接 Nacos" 的错误？**
A: 如果你不使用 Nacos 做服务发现，请修改 `gemini_worker.py`，移除 `get_nacos_target_url` 调用，直接使用固定的 API URL。

**Q: 为什么 DeepSeek R1 响应比较慢？**
A: R1 是推理模型（Reasoning Model），需要进行思维链（CoT）计算。我们在 `deepseek_worker.py` 中将超时时间 `timeout` 设置为了 **300秒** 以适应此特性。

**Q: 任务状态一直显示 PROCESSING？**
A: 检查 Worker 是否正常启动。如果 Worker 崩溃，重启 Worker 即可，它会自动触发 `recover_pending_tasks` 流程，接管并重置这些僵尸任务。
