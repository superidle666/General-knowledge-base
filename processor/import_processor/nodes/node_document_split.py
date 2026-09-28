"""
文档切分节点。
processor/import_processor/nodes/node_document_split.py
按文档层级和长度构建可检索内容块，并保留切片上下文元数据。
"""
import json
import logging
from pathlib import Path
from typing import Tuple, Any, List, Dict

from langchain_text_splitters import RecursiveCharacterTextSplitter

# from app.core.logger import logger
# from app.import_process.agent.node_base import NodeBase
# from app.import_process.agent.state import ImportGraphState

from processor.import_processor.base import BaseNode, setup_logging
from processor.import_processor.content_blocks import ContentBlock, build_content_blocks
from processor.import_processor.state import ImportGraphState

# 切分阈值只有两个来源：ImportConfig 里的默认值，以及知识库级的 chunking_config（见
# _get_chunking_config）。这里不再放一份模块常量当第三套口径。


class NodeDocumentSplit(BaseNode):
    """
    文档切分节点：智能文档切片
    """

    name = "node_document_split"

    def process(self, state: ImportGraphState) -> ImportGraphState:
        """
        节点：文档切分（node_document_split）
        整体流程：加载输入→按MD标题初切→长切短合→统计输出→结果备份
        核心目的：将长MD文档切分为长度适中的Chunk，适配大模型上下文窗口和向量检索
        后续扩展点：可在各步骤间新增Chunk元信息补充、自定义切分规则、向量入库前置处理等

        必要参数：task_id、md_path(完整流程中非必要，备份测试用的json文件)、md_content、file_title
        更新参数：chunks

        :param state: 工作流状态对象
        :return: 更新后的状态对象
        """

        # ===================================== 步骤1：加载并标准化输入数据 =====================================
        # 作用：从状态字典提取MD内容/文件标题，统一换行符消除系统差异
        # 输出：标准化后的md_content、文件标题；
        content, file_title = self._step_1_get_inputs(state)

        # ===================================== 步骤2：构建内容块并按标题块初次切分 =====================================
        # 作用：先保留段落、表格、图片、OCR 等来源结构，再由标题块组织章节。
        content_blocks = self._build_content_blocks(state, content)
        state["content_blocks"] = content_blocks
        state["image_metadata"] = self._collect_image_metadata(content_blocks)
        sections, title_count, lines_count = self._step_2_split_by_blocks(content_blocks, file_title)

        # ===================================== 步骤3：无标题场景兜底处理 =====================================
        # 作用：解决MD文档无任何标题的边界情况，避免后续切分逻辑异常
        # 输出：有标题则返回步骤2的章节列表；无标题则将全文封装为单个「无标题」章节，保证数据格式统一
        sections = self._step_3_handle_no_title_from_blocks(content_blocks, sections, title_count, file_title)

        # ===================================== 步骤4：Chunk精细化处理（长切短合） =====================================
        # 作用：核心切分逻辑，先将超长章节按「段落→句子」二次切分，再合并同父标题的过短章节，减少碎片化
        # 额外处理：对所有Chunk做parent_title兜底，适配Milvus向量库必填字段要求
        # 输出：长度适中、语义完整、低碎片化的最终Chunk列表（可直接用于向量入库/大模型调用）
        max_content_length, min_content_length, chunk_overlap = self._get_chunking_config(state)
        sections = self._step_4_refine_chunks(
            sections,
            content_blocks=content_blocks,
            max_content_length=max_content_length,
            min_content_length=min_content_length,
            chunk_overlap=chunk_overlap,
        )
        sections = self._attach_chunk_metadata(state, sections)

        # ===================================== 步骤5：输出文档切分统计信息 =====================================
        # 作用：打印核心统计数据，便于监控切分效果、调试问题（原始行数/最终Chunk数/首个Chunk预览）
        # 输出：无返回值，仅通过logger输出标准化统计日志
        self._step_5_print_stats(lines_count, sections)

        # ===================================== 步骤6：Chunk结果本地JSON备份 + 状态更新 =====================================
        # 作用：将最终Chunk列表备份到local_dir目录的chunks.json，便于后续问题排查、数据复用
        # 输出：无返回值
        self._step_6_backup(state, sections)

        # 写入状态字典
        state["chunks"] = sections

        return state

    def _get_chunking_config(self, state: ImportGraphState) -> tuple[int, int, int]:
        """读取知识库级切分参数；非法值直接抛错，避免默默降级后产出无法回溯的切块。"""
        snapshot = state.get("config_snapshot") or {}
        chunking_config = snapshot.get("chunking_config") or {}
        max_content_length = int(chunking_config.get("chunk_size", self.config.max_content_length))
        min_content_length = int(chunking_config.get("min_content_length", self.config.min_content_length))
        chunk_overlap = int(chunking_config.get("chunk_overlap", 0))
        if max_content_length < 1:
            raise ValueError("chunk_size 必须为正整数")
        if min_content_length < 0:
            raise ValueError("min_content_length 不能为负数")
        if chunk_overlap < 0 or chunk_overlap >= max_content_length:
            raise ValueError("chunk_overlap 不能为负数，且必须小于 chunk_size")
        return max_content_length, min_content_length, chunk_overlap

    def _build_content_blocks(self, state: ImportGraphState, content: str) -> list[ContentBlock]:
        """将 Markdown、PDF 解析文本或独立 OCR 文本标准化为统一内容块。"""
        source_file = state.get("md_path") or state.get("import_file_path") or ""
        source_kind = "pdf_markdown" if state.get("is_pdf_read_enabled") else "markdown"
        blocks = build_content_blocks(
            content,
            source_file=source_file,
            source_kind=source_kind,
            ocr_content=state.get("ocr_content", ""),
        )
        if not blocks:
            raise ValueError("未从文档中解析到任何内容块")
        self.logger.info(
            "内容块解析完成：blocks=%s types=%s",
            len(blocks),
            ", ".join(sorted({block["block_type"] for block in blocks})),
        )
        return blocks

    def _step_2_split_by_blocks(
        self,
        blocks: list[ContentBlock],
        file_title: str,
    ) -> Tuple[List[Dict[str, Any]], int, int]:
        """按内容块中的标题组织章节，保留块顺序、类型、标题路径与页码。"""
        sections: list[dict[str, Any]] = []
        current_title = ""
        current_title_path: list[str] = []
        current_parts: list[str] = []
        current_blocks: list[ContentBlock] = []
        title_count = 0

        def flush_section() -> None:
            if not current_parts:
                return
            sections.append({
                "title": current_title,
                "content": "\n\n".join(current_parts),
                "file_title": file_title,
                "title_path": list(current_title_path),
                "parent_title": current_title_path[-2] if len(current_title_path) > 1 else file_title,
                "source_page": next(
                    (block.get("page_number") for block in current_blocks if block.get("page_number") is not None),
                    None,
                ),
                "content_block_orders": [block["order"] for block in current_blocks],
                "content_block_types": list(dict.fromkeys(block["block_type"] for block in current_blocks)),
                "chunk_type": ",".join(dict.fromkeys(block["block_type"] for block in current_blocks)),
                "source_kind": ",".join(dict.fromkeys(block.get("source_kind", "") for block in current_blocks if block.get("source_kind"))),
                "image_sources": [
                    block["image_source"]
                    for block in current_blocks
                    if block.get("block_type") == "image" and block.get("image_source")
                ],
                "image_contexts": [
                    block.get("image_context", "")
                    for block in current_blocks
                    if block.get("block_type") in {"image", "image_caption"}
                ],
                "image_metadata": self._collect_image_metadata(current_blocks),
            })

        for block in blocks:
            if not block.get("primary", True):
                continue
            if block["block_type"] == "heading":
                flush_section()
                current_title = block["raw_content"].strip()
                current_title_path = list(block["title_path"])
                current_parts = [current_title]
                current_blocks = [block]
                title_count += 1
                self.logger.info("识别标题：%s", current_title)
                continue

            raw_content = block.get("raw_content", "").strip()
            if raw_content:
                current_parts.append(raw_content)
                current_blocks.append(block)

        flush_section()
        self.logger.info(
            "文档粗切（按内容块标题切分）完成，共%s个章节，标题数量是%s，内容块共有%s个",
            len(sections),
            title_count,
            len(blocks),
        )
        return sections, title_count, len(blocks)

    def _attach_chunk_metadata(self, state: ImportGraphState, sections: List[Dict[str, str]]) -> List[Dict[str, Any]]:
        """补齐知识库、文档、版本与实体等定位字段；这些字段是检索后回表取来源的唯一依据。"""
        import_path = state.get("import_file_path") or ""
        file_name = Path(import_path).name or state.get("file_title", "")
        for chunk_index, section in enumerate(sections):
            section["chunk_index"] = chunk_index
            section["knowledge_base_id"] = state.get("knowledge_base_id") or state.get("library_id")
            section["library_id"] = state.get("library_id") or state.get("knowledge_base_id")
            section["document_id"] = state.get("document_id")
            section["document_version"] = state.get("document_version", 1)
            section["file_name"] = file_name
            section["item_name"] = section.get("item_name", state.get("item_name", "")) or ""
            section["entity_recognition_mode"] = state.get("entity_recognition_mode", "disabled")
        return sections

    @staticmethod
    def _collect_image_metadata(blocks: list[ContentBlock]) -> list[dict[str, Any]]:
        """Collect image source, order, page, and nearby caption metadata."""
        captions = {
            block.get("image_order"): block.get("content", "")
            for block in blocks
            if block.get("block_type") == "image_caption"
        }
        return [
            {
                "source": block.get("image_source", ""),
                "order": block.get("image_order"),
                "page_number": block.get("page_number"),
                "context": block.get("image_context", ""),
                "caption": captions.get(block.get("image_order"), ""),
            }
            for block in blocks
            if block.get("block_type") == "image"
        ]

    def _step_3_handle_no_title_from_blocks(
        self,
        blocks: list[ContentBlock],
        sections: List[Dict[str, Any]],
        title_count: int,
        file_title: str,
    ) -> List[Dict[str, Any]]:
        """无标题时仍以内容块为输入，避免回退为整篇 Markdown 字符串。"""
        if title_count:
            self.logger.debug("步骤3：检测到%s个有效标题，无需兜底处理", title_count)
            return sections

        primary_blocks = [block for block in blocks if block.get("primary", True)]
        self.logger.warning("步骤3：未识别到任何MD标题，将内容块聚合为无标题章节，文件：%s", file_title)
        return [{
            "title": "无标题",
            "content": "\n\n".join(
                block["raw_content"].strip()
                for block in primary_blocks
                if block.get("raw_content", "").strip()
            ),
            "file_title": file_title,
            "title_path": [],
            "parent_title": file_title,
            "source_page": next(
                (block.get("page_number") for block in primary_blocks if block.get("page_number") is not None),
                None,
            ),
            "content_block_orders": [block["order"] for block in primary_blocks],
            "content_block_types": list(dict.fromkeys(block["block_type"] for block in primary_blocks)),
            "chunk_type": ",".join(dict.fromkeys(block["block_type"] for block in primary_blocks)),
            "source_kind": ",".join(dict.fromkeys(block.get("source_kind", "") for block in primary_blocks if block.get("source_kind"))),
            "image_metadata": self._collect_image_metadata(primary_blocks),
            "image_sources": [
                block["image_source"]
                for block in primary_blocks
                if block.get("block_type") == "image" and block.get("image_source")
            ],
            "image_contexts": [
                block.get("image_context", "")
                for block in primary_blocks
                if block.get("block_type") in {"image", "image_caption"}
            ],
        }]

    def _step_1_get_inputs(self, state: ImportGraphState) -> Tuple[str, str]:
        """
        【步骤1】获取并预处理输入数据
        功能：从状态字典中提取MD内容/文件标题/最大长度，做基础标准化
        :param state: 项目状态字典（ImportGraphState），包含md_content等核心键
        :return: 标准化后的MD内容/文件标题（无内容则返回None,None）
        """

        # 中间节点也可以只做轻量级的防御性判断，不做完整的磁盘级别的检查 => 前置校验 + 信任状态
        # 1、file_title非空校验
        file_title = state.get("file_title", "")
        if not file_title:
            raise ValueError("核心参数file_title缺失")

        # 2、从状态中提取MD原始内容
        md_content = state.get("md_content", "")
        if not md_content:
            raise ValueError("核心参数 md_content 缺失")

        # 3、基础标准化：统一换行符，避免Windows/Linux换行符差异导致的后续处理异常
        # 原始混合换行："# HL3070说明书\r\n## 产品概述\nHL3070是扫描枪\r\n\r\n### 操作步骤"
        # 统一后："# HL3070说明书\n## 产品概述\nHL3070是扫描枪\n\n### 操作步骤"
        md_content = md_content.replace("\r\n", "\n").replace("\r", "\n")

        self.logger.info(f"步骤1：输入数据加载完成，文件标题：{file_title}")
        return md_content, file_title

    def _step_4_refine_chunks(
        self,
        sections: List[Dict[str, Any]],
        content_blocks: list[ContentBlock],
        max_content_length: int,
        min_content_length: int,
        chunk_overlap: int,
    ) -> List[Dict[str, Any]]:
        """
        【步骤4】Chunk精细化处理（核心：长切短合，适配大模型/检索）
        执行流程：1.切分超长章节 2.合并过短章节 3.父标题兜底（适配Milvus向量库schema）
        :param sections: 步骤3处理后的章节列表
        :return: 长度适中、低碎片化的最终Chunk列表
        """

        block_lookup = {block["order"]: block for block in content_blocks}
        refined_split: list[dict[str, Any]] = []
        for sec in sections:
            refined_split.extend(self._split_long_section(
                sec,
                max_content_length=max_content_length,
                chunk_overlap=chunk_overlap,
                block_lookup=block_lookup,
            ))
        self.logger.info(f"步骤4-1：超长章节切分完成，共生成{len(refined_split)}个初始子Chunk")

        # 阶段2：合并过短章节 → 减少碎片化，提升后续检索/大模型调用效果
        final_sections = self._merge_short_sections(
            refined_split,
            min_content_length=min_content_length,
            max_content_length=max_content_length,
        )
        self.logger.info(f"步骤4-2：过短章节合并完成，最终得到{len(final_sections)}个Chunk")

        # 阶段3：父标题兜底 → 适配Milvus向量库schema（parent_title为必填字段）
        # 兜底规则：无parent_title则用自身title，title也无则填空字符串
        for sec in final_sections:
            if not sec.get("parent_title"):
                sec["parent_title"] = sec.get("title") or ""
        self.logger.debug("步骤4-3：父标题兜底完成，所有Chunk均包含parent_title字段")

        return final_sections

    def _split_long_section(
        self,
        section: Dict[str, Any],
        *,
        max_content_length: int,
        chunk_overlap: int,
        block_lookup: dict[int, ContentBlock],
    ) -> List[Dict[str, Any]]:
        """优先在内容块边界切分；单个超长段落才使用递归字符切分兜底。"""
        content = str(section.get("content", ""))
        if len(content) <= max_content_length:
            return [section]

        title = str(section.get("title", ""))
        prefix = f"{title}\n\n" if title else ""
        available_length = max_content_length - len(prefix)
        if available_length <= 0:
            self.logger.warning("章节标题过长，保留原章节：%s", title[:20])
            return [section]

        section_blocks = [
            block_lookup[order]
            for order in section.get("content_block_orders", [])
            if order in block_lookup and block_lookup[order].get("primary", True)
        ]
        body_blocks = [block for block in section_blocks if block["block_type"] != "heading"]
        if not body_blocks:
            return [section]

        result: list[dict[str, Any]] = []
        pending_parts: list[str] = []
        pending_blocks: list[ContentBlock] = []

        def append_chunk(parts: list[str], used_blocks: list[ContentBlock]) -> None:
            if not parts:
                return
            part_number = len(result) + 1
            result.append({
                **section,
                "title": f"{title}-{part_number}" if title else f"chunk-{part_number}",
                "content": (prefix + "\n\n".join(parts)).strip(),
                "part": part_number,
                "content_block_orders": [block["order"] for block in used_blocks],
                "content_block_types": list(dict.fromkeys(block["block_type"] for block in used_blocks)),
                "source_page": next(
                    (block.get("page_number") for block in used_blocks if block.get("page_number") is not None),
                    section.get("source_page"),
                ),
            })

        for block in body_blocks:
            raw_content = block.get("raw_content", "").strip()
            if not raw_content:
                continue
            projected_length = len("\n\n".join([*pending_parts, raw_content]))
            if pending_parts and projected_length > available_length:
                append_chunk(pending_parts, pending_blocks)
                pending_parts, pending_blocks = [], []

            if len(raw_content) <= available_length:
                pending_parts.append(raw_content)
                pending_blocks.append(block)
                continue

            if block["block_type"] == "table":
                append_chunk(pending_parts, pending_blocks)
                pending_parts, pending_blocks = [], []
                table_rows = list(block.get("table_rows", []))
                table_header = str(block.get("table_header_content", "")).strip()
                if not table_rows or not table_header:
                    self.logger.warning("表格缺少结构化行信息，完整保留为单个 Chunk：%s", title or "无标题")
                    append_chunk([raw_content], [block])
                    continue

                table_parts: list[str] = [table_header]
                for row in table_rows:
                    candidate = "\n".join([*table_parts, row])
                    if len(candidate) > available_length and len(table_parts) > 1:
                        append_chunk(["\n".join(table_parts)], [block])
                        table_parts = [table_header, row]
                    else:
                        table_parts.append(row)
                if len(table_parts) > 1:
                    append_chunk(["\n".join(table_parts)], [block])
                continue

            append_chunk(pending_parts, pending_blocks)
            pending_parts, pending_blocks = [], []
            splitter = RecursiveCharacterTextSplitter(
                chunk_size=available_length,
                chunk_overlap=min(chunk_overlap, max(0, available_length - 1)),
                separators=["\n\n", "\n", "。", "！", "？", "；", ".", "!", "?", ";", " "],
            )
            for text in splitter.split_text(raw_content):
                normalized_text = text.strip()
                if normalized_text:
                    append_chunk([normalized_text], [block])

        append_chunk(pending_parts, pending_blocks)
        self.logger.debug("内容块超长切分完成：%s → 生成%s个子Chunk", title, len(result))
        return result or [section]

    def _merge_short_sections(
        self,
        sections: List[Dict[str, Any]],
        min_content_length: int,
        max_content_length: int,
    ) -> List[Dict[str, Any]]:
        """
        【辅助函数】过短章节合并（减少碎片化，提升检索效果）
        核心规则：仅合并「同父标题」且「当前块长度不足阈值」的相邻Chunk，避免跨章节合并
        :param sections: 待合并的Chunk列表（通常是_split_long_section切分后的结果）
        :return: 合并后的Chunk列表，长度适中，保留元信息
        """
        # 边界处理：空列表直接返回，避免后续索引报错
        if not sections:
            self.logger.debug("待合并Chunk列表为空，直接返回")
            return []

        merged_sections = []  # 最终合并结果
        current_chunk = None  # 迭代累加器：保存当前待合并的Chunk

        for sec in sections:
            # 初始化：第一个Chunk直接作为当前待合并块
            if current_chunk is None:
                current_chunk = sec
                continue

            # 合并条件：同一标题路径、当前块过短且合并后不超过长度上限。
            is_current_short = len(current_chunk["content"]) < min_content_length
            is_same_parent = current_chunk.get("parent_title") == sec.get("parent_title")
            is_same_title_path = current_chunk.get("title_path") == sec.get("title_path")
            merged_length = len(current_chunk["content"]) + 2 + len(sec["content"])

            if is_current_short and is_same_parent and is_same_title_path and merged_length <= max_content_length:
                # 合并前清理：去掉下一块开头重复的父标题，避免内容冗余
                parent_title = sec.get("parent_title", "")
                next_content = sec["content"]
                if parent_title and next_content.startswith(parent_title):
                    next_content = next_content[len(parent_title):].lstrip()
                # 合并内容：空行分隔，保证格式整洁
                current_chunk["content"] += "\n\n" + next_content
                # 更新子Chunk序号：保留最新序号，便于溯源
                if "part" in sec:
                    current_chunk["part"] = sec["part"]
                for key in ("content_block_orders", "content_block_types"):
                    current_values = current_chunk.get(key)
                    next_values = sec.get(key)
                    if isinstance(current_values, list) and isinstance(next_values, list):
                        current_chunk[key] = list(dict.fromkeys([*current_values, *next_values]))
                self.logger.debug(
                    f"合并短Chunk：{current_chunk.get('parent_title')} → 累计长度{len(current_chunk['content'])}")
            else:
                # 不满足合并条件：将当前块加入结果，切换为新的待合并块
                merged_sections.append(current_chunk)
                current_chunk = sec

        # 循环结束后，将最后一个待合并块加入结果
        if current_chunk is not None:
            merged_sections.append(current_chunk)

        self.logger.debug(f"短Chunk合并完成：原{len(sections)}个 → 合并后{len(merged_sections)}个")
        return merged_sections

    def _step_5_print_stats(self, lines_count: int, sections: List[Dict[str, str]]) -> None:
        """
        【步骤5】输出文档切分统计信息（日志记录，便于监控/调试）
        :param lines_count: MD原始文本总行数
        :param sections: 最终处理后的Chunk列表
        """
        chunk_num = len(sections)
        # 输出核心统计信息：原始行数/最终Chunk数/首个Chunk预览
        self.logger.info("-" * 50 + " 文档切分统计信息 " + "-" * 50)
        self.logger.info(f"MD原始文本总行数：{lines_count}")
        self.logger.info(f"最终生成Chunk数量：{chunk_num}")

    def _step_6_backup(self, state: ImportGraphState, sections: List[Dict[str, str]]) -> None:
        """
        【步骤6】Chunk结果本地JSON备份（便于调试/问题排查，保留处理结果）
        :param state: 项目状态字典，需包含md_dir（备份目录）
        :param sections: 最终处理后的Chunk列表
        """

        try:
            # 拼接备份文件路径：固定文件名，便于查找
            knowledge_base_id = state.get("knowledge_base_id") or state.get("library_id") or "unknown-kb"
            document_id = state.get("document_id") or "unknown-document"
            document_version = state.get("document_version", 1)
            backup_name = f"chunks-kb-{knowledge_base_id}-doc-{document_id}-v-{document_version}.json"
            backup_path = Path(state["md_path"]).parent / backup_name
            # 写入JSON文件：保留中文/格式化缩进，便于人工查看
            with open(backup_path, "w", encoding="utf-8") as f:
                """
                sections是Python 嵌套数据结构（List[Dict[str, str]]，列表里装字典，字典里可能嵌套字符串 / 数字等），而普通文件写入
                （如f.write(sections)）仅支持写入字符串，直接写 Python 数据结构会报错。
                json.dump的核心作用就是：将 Python 原生数据结构（列表、字典、字符串、数字等）直接序列化并写入 JSON 文件，无需手动转换为字符串，
                同时保证数据格式规范、可跨语言 / 跨场景读取，完美适配「Chunk 列表备份」的需求。
                """
                json.dump(
                    sections,
                    f,
                    # 开启 True："title": "\u4e00\u7ea7\u6807\u9898"（乱码，无法直接看）；
                    # 开启 False："title": "一级标题"（正常中文，人工可直接阅读）。
                    ensure_ascii=False,  # 保留中文，不转义为\u编码
                    indent=2  # 格式化缩进，便于阅读
                )
            self.logger.info(f"步骤6：Chunk结果备份成功，备份文件路径：{backup_path}")
        except Exception as e:
            # 备份失败仅记录日志，不终止主流程
            self.logger.error(f"步骤6：Chunk结果备份失败，错误信息：{str(e)}", exc_info=False)
if __name__ == "__main__":

    setup_logging()

    md_path = r"E:\output\H3C LA2608室内无线网关 用户手册-6W100-整本手册\H3C LA2608室内无线网关 用户手册-6W100-整本手册_new.md"
    with open(md_path, "r", encoding="utf-8") as f:
        md_content = f.read()

    init_state = {
        "md_path": md_path,
        "md_content": md_content,
        "file_title": "H3C LA2608室内无线网关 用户手册-6W100-整本手册"
    }

    # 执行核心处理流程
    node_document_split= NodeDocumentSplit()
    result = node_document_split(init_state)

    logging.getLogger().info( json.dumps(result, ensure_ascii=False, indent=4))
