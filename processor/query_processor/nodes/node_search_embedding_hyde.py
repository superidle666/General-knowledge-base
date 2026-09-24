"""
HyDE 向量检索节点。

@FilePath: processor/query_processor/nodes/node_search_embedding_hyde.py
@Date: 2026-09-07
@Description: 先生成假设性答案，再将其向量化用于补充知识库召回。
"""
from config.milvus_config import milvus_config
from processor.query_processor.base import NodeBase
from processor.query_processor.prompt.search_embedding_hyde import HYDE_PROMPT
from processor.query_processor.retrieval_common import (
    HYBRID_RANKER_WEIGHTS,
    LIBRARY_FILTER_FIELD,
    build_item_name_filter,
    build_library_filter,
    collection_field_names,
    combine_filters,
    normalize_hits,
    resolve_output_fields,
)
from processor.query_processor.state import QueryGraphState
from tool.logger import logger
from utils.embedding_utils import generate_embeddings
from utils.json_format_utils import serialize_json
from utils.llm_utils import get_llm_client
from utils.milvus_utils import create_hybrid_search_requests, get_milvus_client, hybrid_search


class NodeSearchEmbeddingHyde(NodeBase):
    """
    节点功能：HyDE (Hypothetical Document Embedding)
    先让 LLM 生成假设性答案，再对答案进行向量检索，提高召回率。
    """

    # 覆盖基类的 name 属性，标识节点名称
    name: str = "node_search_embedding_hyde"

    def process(self, state: QueryGraphState) -> QueryGraphState:
        """
        HyDE (Hypothetical Document Embedding) 检索节点
        核心思想：通过LLM生成假设性答案（HyDE文档），将其向量化后用于检索，以解决短查询语义稀疏问题。

        执行步骤：
        1. 前置校验：问题为空或 library_id 无效时直接返回空结果，不白烧一次大模型调用。
        2. 生成假设文档 (Step 1)：调用LLM，基于用户问题生成一段假设性的理想回答（即HyDE文档）。
        3. 混合检索 (Step 2)：
           - 将“用户问题 + 假设文档”合并，生成BGE-M3稠密+稀疏向量。
           - 在与本地检索同一个集合上执行混合检索（带知识库隔离与实体过滤）。
        4. 结果封装：返回归一化后的切片列表、假设文档原文，以及本路是否失败的标记。

        失败一律降级为空召回并置 hyde_failed，不能让整个问答请求因增强链路失败而失败：
        此时 RRF 只剩本地一路，答案质量下降但仍可回答。

        :param state: 会话状态字典，包含 rewritten_query, library_id, item_names 等
        :return: 包含 hyde_embedding_chunks、hyde_doc 与 hyde_failed 的字典
        """

        # 1、用户问题、目标知识库和已确认实体名
        rewritten_query = str(state.get("rewritten_query") or state.get("original_query") or "").strip()
        library_id = state.get("library_id")
        item_names = state.get("item_names") or []

        if not rewritten_query:
            logger.info("步骤1: 无有效问题，跳过 HyDE 检索")
            return {"hyde_embedding_chunks": [], "hyde_failed": False}

        if not isinstance(library_id, int) or library_id <= 0:
            logger.warning("步骤1: library_id 无效，跳过 HyDE 检索：%s", library_id)
            return {"hyde_embedding_chunks": [], "hyde_failed": True}

        try:

            # 2、生成假设性文档
            hyde_doc = self._step_1_create_hyde_doc(rewritten_query)

            # 3、用“重写问题 + 假设文档”检索切片
            res = self._step_2_search_embedding_hyde(
                rewritten_query=rewritten_query,
                hyde_doc=hyde_doc,
                library_id=library_id,
                item_names=item_names,
                retrieval_top_k=int(state.get("retrieval_top_k") or 20),
            )

            # 4、结果封装
            return {
                "hyde_embedding_chunks": res,
                "hyde_doc": hyde_doc,
                "hyde_failed": False,
            }

        except Exception as e:
            logger.exception(f"假设性文档向量搜索失败，本路召回置空: {e}")
            return {"hyde_embedding_chunks": [], "hyde_failed": True}

    def _step_1_create_hyde_doc(self, rewritten_query: str) -> str:
        """
        阶段1：利用大模型根据用户查询生成假设性文档（Hypothetical Document）。
        HyDE的核心在于：利用LLM生成一个“虚构但相关”的文档，用该文档的向量去检索真实的文档，
        从而缓解短查询（Query）与长文档（Document）在语义空间不匹配的问题。

        :param rewritten_query: 用户改写后的查询语句
        :return: LLM生成的假设性文档内容
        """

        logger.info("步骤1: 开始生成假设性文档")

        try:
            llm = get_llm_client()
            hyde_prompt = HYDE_PROMPT.format(rewritten_query=rewritten_query)
            hyde_doc = llm.invoke(hyde_prompt).content
            return hyde_doc

        except Exception as e:
            logger.exception(f"步骤1: 生成假设文档失败: {e}")
            raise e

    def _step_2_search_embedding_hyde(
            self,
            rewritten_query: str,
            hyde_doc: str,
            library_id: int,
            item_names=None,
            retrieval_top_k: int = 20,
    ):
        """
        阶段2：利用“重写问题 + 假设性文档”生成 embedding，并到向量库检索切片。

        检索集合、输出字段和过滤条件都必须与本地检索一致：两路结果随后要在 RRF 里
        按 chunk_id 去重合并，字段不一致会让其中一路的来源无法回填。

        :param rewritten_query: 改写后的查询
        :param hyde_doc: Step 1 生成的假设性文档
        :param library_id: 目标知识库 ID，用于库隔离
        :param item_names: 实体名称列表，用于元数据过滤 (item_name in [...])
        :param retrieval_top_k: 底层召回数量，后续交给重排序收窄
        :return: 归一化后的检索结果列表
        """

        # 1、拼接查询与假设文档，形成更丰富的语义上下文
        # 这里把用户问题 + 假设答案拼在一起生成向量，相当于：
        # 既保留了用户的原始意图（rewritten_query）
        # 又增强了语义丰富度（hyde_doc）
        combined_text = f"{rewritten_query}\n{hyde_doc}".strip()

        # 2、生成向量 (Dense + Sparse)
        embeddings = generate_embeddings([combined_text])
        dense_vec = embeddings["dense"][0]
        sparse_vec = embeddings["sparse"][0]

        # 3、获取Milvus集合：与本地检索共用 v2 集合，v1 老集合已不再由导入流程写入
        collection_name = milvus_config.document_chunks_v2_collection
        client = get_milvus_client()
        if not client.has_collection(collection_name=collection_name):
            logger.warning("步骤2: HyDE 检索集合不存在：%s", collection_name)
            return []

        field_names = collection_field_names(client, collection_name)
        if LIBRARY_FILTER_FIELD not in field_names:
            raise ValueError(f"检索集合缺少 {LIBRARY_FILTER_FIELD} 字段：{collection_name}")

        # 4、构造过滤条件：库隔离必须保留，实体名按集合结构可选附加并做转义
        expr = combine_filters(
            build_library_filter(library_id),
            build_item_name_filter(item_names, field_names),
        )
        logger.info(f"步骤2: 过滤条件: {expr}")

        # 5、构造Milvus混合搜索请求对象
        # 底层召回数量与本地检索对齐（后续会再收窄，预留更多结果做重排序）
        search_limit = max(int(retrieval_top_k or 20), 10)
        reqs = create_hybrid_search_requests(
            dense_vector=dense_vec,
            sparse_vector=sparse_vec,
            expr=expr,
            limit=search_limit,
        )

        # 6、执行混合向量检索
        logger.info("步骤2: 开始执行 Milvus 混合检索...")
        res = hybrid_search(
            client=client,
            collection_name=collection_name,
            reqs=reqs,
            ranker_weights=HYBRID_RANKER_WEIGHTS,
            limit=search_limit,
            output_fields=resolve_output_fields(field_names),
        )

        return normalize_hits(res)


if __name__ == "__main__":

    init_state = {
        "rewritten_query": "关于brother HAK180烫金机，如何调节转印温度？",
        "library_id": 1,  # 本地实际知识库 ID，手工验证时按库修改
        "item_names": ["BrotherHAK180烫金机", "BrotherHAK-180烫金机"]
    }
    node_search_embedding_hyde = NodeSearchEmbeddingHyde()
    result = node_search_embedding_hyde(init_state)
    logger.info(serialize_json(result, indent=4))
