# 🚀 AI Task System (Async)

这是一个异步 AI 任务处理系统，旨在解耦 API 接口与耗时的 AI 模型推理过程。它支持文本对话（Chat）和图像生成（Image Generation）任务，具备会话管理、历史记录查询以及多模型路由功能。

## ✨ 主要功能

* **异步架构**：利用 Redis 消息队列解耦任务提交与执行，防止 API 阻塞。
* **多模型支持**：自动根据模型名称（Gemini, Stable Diffusion, DeepSeek 等）将任务分发到不同的处理队列。
* **会话管理 (Conversation)**：支持基于 `conversation_id` 的上下文维护，可查询历史对话记录。
* **双重任务类型**：
* **Text (Chat)**：流式或非流式文本对话支持。
* **Image**：图像生成任务支持。


* **持久化存储**：使用 PostgreSQL 存储任务状态、结果及会话元数据。

## 📂 项目结构

```text
ai-task-system/
├── api-gateway/
│   └── main.py              # FastAPI 入口，负责接收请求并推送到 Redis
├── init/
│   └── init_db.py           # 数据库初始化脚本（创建表结构）
├── shared/                  # 共享模块
│   ├── database.py          # 数据库连接配置
│   ├── models.py            # SQLAlchemy ORM 模型定义
│   └── schemas.py           # Pydantic 数据验证模型
├── workers/
│   └── gemini/
│       └── mock_worker.py   # 后台 Worker，监听 Redis 并处理任务
├── .env                     # 环境变量配置文件
├── .gitignore               # Git 忽略配置
├── requirements.txt         # 项目依赖列表
└── README.md                # 项目说明文档

```

## 🛠️ 技术栈

* **Web 框架**: FastAPI
* **数据库**: PostgreSQL (SQLAlchemy + psycopg2)
* **消息队列**: Redis
* **语言**: Python 3.9+

## ⚙️ 环境准备与安装

### 1. 克隆项目与安装依赖

```bash
# 进入项目目录
cd ai-task-system

# 创建虚拟环境 (推荐)
python -m venv venv
source venv/bin/activate  # Linux/Mac
# venv\Scripts\activate   # Windows

# 安装依赖
pip install -r requirements.txt

```

### 2. 配置环境变量

在项目根目录创建一个 `.env` 文件（参考已有的配置），并填入你的数据库和 Redis 信息：

```ini
# .env 文件
# --- 数据库配置 (PostgreSQL) ---
DB_HOST=127.0.0.1
DB_PORT=5432
POSTGRES_DB=gemini
POSTGRES_USER=postgres
POSTGRES_PASSWORD=your_password

# --- Redis 配置 ---
REDIS_HOST=127.0.0.1
REDIS_PORT=6379

```

### 3. 初始化数据库

在运行程序之前，需要先创建数据库表结构：

```bash
# 运行初始化脚本
python init/init_db.py

```

*成功运行后会提示：✅ 数据库表结构同步完成！*

## 🚀 启动服务

你需要开启两个终端窗口，分别运行 **API 服务** 和 **Worker 服务**。

### 终端 1：启动 API 网关

```bash
# 默认运行在 http://127.0.0.1:8000
python api-gateway/main.py

```

或者使用 uvicorn 命令：

```bash
uvicorn api-gateway.main:app --reload

```

### 终端 2：启动后台 Worker

```bash
# 监听 Redis 队列并模拟处理任务
python workers/gemini/mock_worker.py

```

## 🔌 API 接口说明

服务启动后，可访问 Swagger 文档查看完整接口：`http://127.0.0.1:8000/docs`

### 1. 发起文本对话 (Chat)

* **Endpoint**: `POST /v1/chat/completions`
* **Body**:
```json
{
  "prompt": "你好，请介绍一下你自己",
  "model": "gemini-2.5-flash",
  "conversation_id": null 
}

```


*注意：如果不传 `conversation_id`，系统会自动创建一个新会话并返回 ID。*

### 2. 发起图片生成 (Image)

* **Endpoint**: `POST /v1/images/generations`
* **Body**:
```json
{
  "prompt": "一只在太空骑自行车的猫",
  "model": "stable-diffusion-3",
  "conversation_id": "可选的会话ID"
}

```



### 3. 查询任务状态

由于是异步处理，提交任务后会立即得到一个 `task_id`。使用此接口轮询结果。

* **Endpoint**: `GET /v1/tasks/{task_id}`

### 4. 获取会话历史

* **Endpoint**: `GET /v1/conversations/{conversation_id}/history`

## 🧠 核心逻辑说明

1. **任务分发 (`api-gateway/main.py`)**:
* 当 API 收到请求时，会根据 `model` 字段的名称关键词（如 `gemini`, `sd`, `deepseek`）决定将任务推送到 Redis 的哪个 List（队列）中。


2. **任务处理 (`workers/gemini/mock_worker.py`)**:
* Worker 使用 `brpop` 阻塞式监听多个队列。
* 一旦获取到任务，Worker 会模拟处理时间（`sleep`），然后将结果（文本或图片URL）回写到 PostgreSQL 数据库，并将任务状态更新为 `SUCCESS`。



## ⚠️ 注意事项

* 目前的 Worker (`mock_worker.py`) 是一个**模拟实现**，它不会真正调用外部 AI API，仅用于测试流程连通性。你需要修改 Worker 代码以接入真实的 OpenAI/Gemini/StableDiffusion API。
* 确保本地已安装并启动了 Redis 和 PostgreSQL 服务。