import os
import requests
from common.logger import debug_log


def upload_files_to_downstream(target_base_url, local_file_paths):
    """
    辅助函数：将本地文件上传到下游 Gemini API
    :param target_base_url: 例如 http://192.168.1.5:8001
    :param local_file_paths: 本地共享卷的文件路径列表
    :return: 下游服务返回的文件路径列表 (remote_files)
    """
    upload_url = f"{target_base_url}/upload"
    remote_files = []

    files_to_send = []
    opened_files = []  # 用于最后关闭文件句柄

    try:
        # 1. 准备 multipart/form-data
        for path in local_file_paths:
            if os.path.exists(path):
                f = open(path, 'rb')
                opened_files.append(f)
                # ('files', (filename, file_object, content_type))
                files_to_send.append(('files', (os.path.basename(path), f, 'application/octet-stream')))
            else:
                debug_log(f"⚠️ 文件不存在，跳过: {path}", "WARNING")

        if not files_to_send:
            return []

        # 2. 发送上传请求
        debug_log(f"正在上传文件到下游: {upload_url}", "REQUEST")
        resp = requests.post(upload_url, files=files_to_send, timeout=60)

        if resp.status_code == 200:
            data = resp.json()
            remote_files = data.get("files", [])
            debug_log(f"✅ 文件中转成功: {remote_files}", "SUCCESS")
        else:
            debug_log(f"❌ 文件上传失败: {resp.text}", "ERROR")

    except Exception as e:
        debug_log(f"❌ 上传过程异常: {e}", "ERROR")
    finally:
        # 关闭所有文件句柄
        for f in opened_files:
            f.close()

    return remote_files