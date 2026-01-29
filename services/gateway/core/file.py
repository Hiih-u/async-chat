import os
import shutil
import uuid
from typing import List
from fastapi import UploadFile
from common.logger import debug_log


def save_uploaded_files(files: List[UploadFile], upload_dir: str) -> List[str]:
    """
    保存上传的文件到指定目录
    """
    saved_paths = []
    if not files:
        return saved_paths

    # 确保目录存在
    os.makedirs(upload_dir, exist_ok=True)

    for file in files:
        # 生成唯一文件名防止冲突
        file_ext = file.filename.split(".")[-1] if "." in file.filename else "tmp"
        file_name = f"{uuid.uuid4()}.{file_ext}"
        file_path = os.path.join(upload_dir, file_name)

        try:
            with open(file_path, "wb") as buffer:
                shutil.copyfileobj(file.file, buffer)

            saved_paths.append(file_path)
            debug_log(f"📂 文件已保存: {file_path}", "INFO")
        except Exception as e:
            debug_log(f"❌ 文件保存失败 {file.filename}: {e}", "ERROR")
            # 可以选择抛出异常或跳过

    return saved_paths