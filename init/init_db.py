# init/init_db.py
import sys
import os
from dotenv import load_dotenv

# --- 关键逻辑开始 ---

# 1. 算出项目根目录的路径 (当前文件的上一级目录)
current_dir = os.path.dirname(os.path.abspath(__file__))  # D:\...\init
project_root = os.path.dirname(current_dir)  # D:\...\AI-task-system

# 2. 把项目根目录加入 Python 搜索路径 (否则 import common 会报错)
sys.path.append(project_root)

# 3. 指定 .env 文件的绝对路径并加载
env_path = os.path.join(project_root, '.env')
if os.path.exists(env_path):
    load_dotenv(env_path)
    print(f"✅ 成功加载环境变量: {env_path}")
else:
    print(f"⚠️  警告: 未找到环境变量文件: {env_path}")

# --- 关键逻辑结束 ---

# 4. 只有在加载完环境变量后，才能导入 common
from common.database import Base, engine
from common import models  # 必须导入 models，否则 create_all 不知道要创建什么表


def init_models():
    print(f"🔌 正在连接数据库: {engine.url.render_as_string(hide_password=True)}")
    print("🛠️  正在检查表结构...")

    # ⚠️ 警告：这会清空所有数据！仅在开发初期使用
    print("🗑️  正在删除旧表 (Drop All)...")
    Base.metadata.drop_all(bind=engine)

    print("🛠️  正在创建新表 (Create All)...")
    Base.metadata.create_all(bind=engine)

    # 创建表
    Base.metadata.create_all(bind=engine)

    print("✅ 数据库表结构同步完成！")


if __name__ == "__main__":
    init_models()