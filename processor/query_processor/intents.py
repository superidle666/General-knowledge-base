"""查询图 intent 取值的唯一登记表（零依赖，可被接口层与图内同时 import）。

intent 原先是散在两个模块里的裸字符串（闲聊闸写 "chitchat"、清单闸写 "inventory_refusal"、
元指令模块自己定义两个常量），没有任何一处能回答"这个字段一共有哪些值"。新增元数据意图一次
就要引入六个值，届时接口、日志、离线统计都要对着字符串比对——把值散开的最坏后果不是报错，
是同一个语义出现两种拼法，然后按字面量写的统计漏掉一半。

集中登记后即可被静态守卫钉住：tests/test_intent_registry.py 断言"源码里所有
`state["intent"] = 字面量` 的取值集合"与本模块的 ALL_INTENTS **全等**，新增取值不登记就红。

默认知识问答**故意不写 intent**（保持键缺失）：现有断言与下游都按"没有意图标签"理解普通问答，
给它补一个 "knowledge" 默认值会牵动一批测试而不带来任何信息。

@FilePath: processor/query_processor/intents.py
"""

from __future__ import annotations

# ---- 既有取值（从各自模块收编过来，字符串值一字未改，历史日志与离线统计仍然对得上）----
INTENT_CHITCHAT = "chitchat"  # 命中闲聊意图闸：固定话术短路，零检索零生成
INTENT_INVENTORY_REFUSAL = "inventory_refusal"  # 库级清单型元问题被前置拒答（R16，已由元数据闸接管）
INTENT_CONVERSATION_ECHO = "conversation_echo"  # 对话回指：短路，但答案由历史回读而非固定话术
INTENT_META_REWRITE = "meta_rewrite"  # 改写型元指令：不短路，只把检索词换成上一轮真问题

# ---- 元数据意图（库级事实问答，全部短路，答案由模板拼接、不经大模型）----
INTENT_METADATA_ENTITY = "metadata_entity"  # 本库有哪些产品/实体（含按品牌过滤）
INTENT_METADATA_DOCUMENT = "metadata_document"  # 本库有哪些文档
INTENT_METADATA_STATS = "metadata_stats"  # 本库有多少份文档/多少个切片
INTENT_METADATA_CONFIG = "metadata_config"  # 本库当前配置（实体识别模式/确认档/HyDE）
INTENT_METADATA_UNSUPPORTED = "metadata_unsupported"  # 问的是库外商业事实（价格/库存/发货）：一句顶死
INTENT_METADATA_UNAVAILABLE = "metadata_unavailable"  # 画像没读到：降级话术，绝不退成"库里没有"

ALL_INTENTS: tuple[str, ...] = (
    INTENT_CHITCHAT,
    INTENT_INVENTORY_REFUSAL,
    INTENT_CONVERSATION_ECHO,
    INTENT_META_REWRITE,
    INTENT_METADATA_ENTITY,
    INTENT_METADATA_DOCUMENT,
    INTENT_METADATA_STATS,
    INTENT_METADATA_CONFIG,
    INTENT_METADATA_UNSUPPORTED,
    INTENT_METADATA_UNAVAILABLE,
)
