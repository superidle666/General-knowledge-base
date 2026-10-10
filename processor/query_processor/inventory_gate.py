"""库级清单型问法的句式判定与回滚话术（R16）。

要解决的问题（用户实测复现）：库里明明有 2 个华为实体，问"当前知识库有哪些华为的产品"
却得到"未明确列出具体的华为产品型号 + 一堆官方渠道链接"。成因不是幻觉：库内每份文档只
描述自己，**不存在"产品清单"这种切片**，于是与这类 query 向量最近的总是手册前言/法律
声明/服务支持段（"本指南仅供参考，产品信息请以实物为准"＋官网链接），模型照着它答，
`sources=5` 看起来还一切正常——比零召回（R5）更隐蔽的答非所问。

**本模块的定位已经变了**：R16 当时用固定话术拒答是成本权衡下的临时止血（库里确实没有
可检索的清单切片），现在 `metadata_gate` 已接走这一格——同样零检索零生成，但不拒答而是
用接口层预载的元数据画像真答出来。本模块因此只剩两件事：
① 保留清单型句式判定（它已验证得够准，元数据闸直接复用，不另起一套正则）；
② 保留 `INVENTORY_REJECT_REPLY` 当 `METADATA_ANSWER_DISABLE=1` 时的回滚出口。

为什么不改成"把本库实体词表枚举给模型"：那要额外注入 token 并必然多一次生成调用，库一
大、并发一高成本就压不住。元数据闸走的是同一条成本路线，只是把"拒答"换成"模板拼接"。

判定刻意**只看句式**（与闲聊闸的知识问法骨架同源思路），实体护栏放在调用方
`node_prepare_query._metadata_gate`：误拒答是用户可见的硬错误，漏拒只是退回现状，
所以护栏一律取"宁查勿否"方向。

@FilePath: processor/query_processor/inventory_gate.py
"""

from __future__ import annotations

import re

from processor.query_processor.chitchat_gate import normalize_for_match

# 固定话术：用户给定的措辞（只把"你"统一成与既有反问/拒答话术一致的"您"）。
INVENTORY_REJECT_REPLY = "当前我无法获取知识库的全部产品，请提供您要咨询的具体型号。"

# 库级范围词：句子把"库本身"当成话题。前缀（当前/你们/这个）无需枚举，匹配用 search，
# 只要这些范围词作为子串出现即可。刻意不收裸"库"——"数据库"里那个库不是本库。
# 公开给 metadata_gate 的计数型/配置型骨架复用：同一个"问的是库本身"的判据只能有一份，
# 两处各写一份词表的后果是"知识库里有多少文档"被认、"库内有多少文档"不被认。
LIBRARY_SCOPE = r"(?:知识库|知识库里|库里|库内|库中|本库|该库|资料库|文档库)"
_LIBRARY_SCOPE = LIBRARY_SCOPE

# 清单/枚举疑问词。注意**不含**"有没有"：那是存在性追问（"知识库里有没有 X 的说明书"），
# 内容检索答得了，不能被本闸吃掉。"多少"也只在带量词（种/款）时才算要清单。
INVENTORY_ASK = (
    r"(?:都?有哪些|都?有什么|都有什么|都有些|收录了?哪些|收录了?什么"
    r"|列出|列举|清单|目录|多少种|多少款)"
)
_INVENTORY_ASK = INVENTORY_ASK

# 清单宾语。刻意不含"功能/参数/配件/说明书"这类**切片里真有**的内容对象：问到它们说明
# 用户要的是文档内容，正常走检索。
INVENTORY_OBJECT = r"(?:产品|商品|设备|机型|型号|资料|文档|实体|品类|种类)"
_INVENTORY_OBJECT = INVENTORY_OBJECT

_INVENTORY_RES = (
    # 型一：库级范围词 + 清单疑问（"知识库里都有哪些型号"）。范围词已经说明问的是库本身，
    # 不再要求宾语。间隔限制在 10 字内，避免长句里远距离凑出骨架。
    re.compile(rf"{_LIBRARY_SCOPE}.{{0,10}}?{_INVENTORY_ASK}"),
    # 型二：无范围词时必须有清单宾语（"有哪些华为产品信息"）。
    re.compile(rf"{_INVENTORY_ASK}.{{0,6}}?{_INVENTORY_OBJECT}"),
    # 型三：名词化的清单请求（"这个知识库的产品清单"）。"的"可省，但宾语与"清单"之间
    # 不许插入其它词，否则"这款产品的说明书目录"会被误吃。
    re.compile(rf"{_INVENTORY_OBJECT}(?:的)?(?:列表|清单|目录)"),
)


def is_library_inventory_question(text: object) -> bool:
    """这句是在要"库的清单"而不是"某个产品的内容"吗？只看句式，不含实体护栏。

    实体护栏（提到具体型号/本库词表里的名字/带指代就不算元问题）必须由调用方结合本库词表
    判断，本函数是纯文本规则，便于单独测试与复用。
    """
    normalized = normalize_for_match(text)
    if not normalized:
        return False
    return any(pattern.search(normalized) for pattern in _INVENTORY_RES)
