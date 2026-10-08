"""检索侧实体抽取提示词。

processor/query_processor/prompt/entity_extract.py
让模型从用户问题及多轮历史里抽取"被询问的产品/型号实体"，输出稳定 JSON。
只做实体抽取、不回答问题；无法确定时返回空数组，交由正则与词表对齐兜底，
宁可漏抽也不能臆造词表里没有的型号，否则会错误触发检索过滤或拒答。
"""

ENTITY_EXTRACT_SYSTEM = (
    "你是设备技术资料知识库的实体抽取器。你的唯一任务是从用户提问中识别出被询问的"
    "产品或型号实体名（例如具体机型、产品系列名）。\n"
    "规则：\n"
    "1. 只抽取问题真正指向的产品/型号，忽略‘怎么’‘如何’‘故障’这类泛词与动作描述；\n"
    "2. 保留用户原文中出现的形式，不要翻译、补全或纠正大小写与连字符；\n"
    "3. 结合历史判断指代（如“它”“这台设备”指向的型号），但不要把历史里未被再次询问的产品也带进来；\n"
    "4. 最多输出 5 个实体；确实无法确定时输出空数组。\n"
    '只输出 JSON，形如 {"entities": ["实体1", "实体2"]}，不要任何解释文字。'
)


def build_entity_extract_prompt(query: str, recent_history: str) -> str:
    """把当前问题与裁剪后的历史拼成用户消息内容；历史为空时只给问题。"""
    history_block = recent_history.strip()
    if history_block:
        return f"最近对话：\n{history_block}\n\n当前问题：{query}"
    return f"当前问题：{query}"
