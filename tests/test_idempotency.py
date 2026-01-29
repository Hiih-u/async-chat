# tests/test_idempotency.py
import sys
import os
import uuid
import time
from threading import Thread

# 添加项目根目录到路径
sys.path.append(os.getcwd())

from common.database import SessionLocal
from common import models
from common.models import TaskStatus
from common.utils.worker_utils import claim_task


def setup_test_task():
    """创建一个测试用的 PENDING 任务"""
    db = SessionLocal()
    task_id = str(uuid.uuid4())
    task = models.Task(
        task_id=task_id,
        prompt="Test Prompt",
        model_name="test-model",
        status=TaskStatus.PENDING
    )
    db.add(task)
    db.commit()
    db.close()
    print(f"📝 创建测试任务: {task_id}")
    return task_id


def simulate_worker(worker_name, task_id, results):
    """模拟一个 Worker 尝试抢任务"""
    db = SessionLocal()
    print(f"👷 {worker_name} 尝试抢占...")

    # 模拟网络延迟，让大家尽量“同时”去抢
    time.sleep(0.1)

    success = claim_task(db, task_id)
    if success:
        print(f"✅ {worker_name} 抢到了！")
        results.append(worker_name)
    else:
        print(f"❌ {worker_name} 抢占失败")

    db.close()


def test_concurrency():
    print("=== 开始并发抢占测试 ===")
    task_id = setup_test_task()
    results = []

    # 启动 5 个线程模拟 5 个 Worker 同时去抢同一个 task_id
    threads = []
    for i in range(5):
        t = Thread(target=simulate_worker, args=(f"Worker-{i + 1}", task_id, results))
        threads.append(t)
        t.start()

    for t in threads:
        t.join()

    print("-" * 30)
    print(f"最终抢到任务的 Worker 数量: {len(results)}")

    if len(results) == 1:
        print("🎉 测试通过：只有一个 Worker 成功抢到了任务！(幂等性验证成功)")
    else:
        print(f"💀 测试失败：竟然有 {len(results)} 个 Worker 抢到了任务！(原子性失效)")


if __name__ == "__main__":
    test_concurrency()