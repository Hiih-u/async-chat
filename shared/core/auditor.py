from shared.utils.logger import debug_log
from shared.core.task_state import finish_task_success, mark_task_failed

def process_ai_result(db, task_id, ai_text, cost_time, conversation_id=None, refusal_keywords=None):
    """
    ⚖️ 通用 AI 结果处理函数 (终审法官)

    1. 软拒绝检测 (Soft Rejection Check): 检查内容是否包含拒绝关键词
    2. 如果命中 -> 自动标记为失败 (FAILED)
    3. 如果通过 -> 自动标记为成功 (SUCCESS) 并保存

    :param refusal_keywords: 拒绝词列表 (List[str])，如果不传则不检查
    :return: True(成功保存), False(被拒绝或出错)
    """
    try:
        # --- 1. 软拒绝检测 ---
        if refusal_keywords:
            # 检查是否包含任意一个关键词
            is_refusal = any(keyword in ai_text for keyword in refusal_keywords)

            if is_refusal:
                error_msg = f"AI 拒绝生成: {ai_text[:100]}..."  # 只截取前100字避免日志过长
                debug_log(f"🛑 捕获到软拒绝: {error_msg}", "WARNING")

                # 直接调用同文件的失败处理函数
                mark_task_failed(db, task_id, f"生成失败: {ai_text}")
                return False

        # --- 2. 审核通过，保存结果 ---
        # 直接调用上一轮我们封装好的成功处理函数
        return finish_task_success(db, task_id, ai_text, cost_time, conversation_id)

    except Exception as e:
        debug_log(f"处理 AI 结果时发生异常: {e}", "ERROR")
        return False
