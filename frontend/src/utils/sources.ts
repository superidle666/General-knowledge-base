/** 用户端检索来源展示归一化。 */

import type { QuerySource } from '../api/query'
import type { UserSource } from '../types/user'

/** 检索来源展示色板（按出现顺序轮换）。 */
const sourceColors = ['var(--primary)', 'var(--accent-green)', 'var(--accent-amber)']

/** 分数口径缺省按 rerank 处理（与后端默认值一致）。 */
function scoreKindOf(source: QuerySource) {
  return source.score_kind ?? 'rerank'
}

/**
 * 把后端检索来源转换为展示数据。
 * 归一到 0~1：rerank 绝对分跨查询不可比，只用"本次查询内相对最高分"做分档基准；
 * 精排降级(recall)时 score 只是召回序投影，不参与归一也不给百分比。
 */
export function normalizeSources(list: QuerySource[]): UserSource[] {
  const normOf = (item: QuerySource): number | null => {
    if (scoreKindOf(item) !== 'rerank' || item.score === null) return null
    return Math.max(0, Math.min(1, item.score > 1 ? item.score / 100 : item.score))
  }
  const maxNorm = list.reduce((acc, item) => Math.max(acc, normOf(item) ?? 0), 0)
  return list.map((source, index) => {
    const isRerank = scoreKindOf(source) === 'rerank'
    const norm = normOf(source)
    // 分档只表相对强弱（很相关/较相关/参考），不表答案正确概率；降级态给中性"参考序"。
    let relevanceLabel: string
    if (!isRerank) relevanceLabel = '参考序'
    else if (norm === null || maxNorm <= 0) relevanceLabel = '参考'
    else {
      const ratio = norm / maxNorm
      relevanceLabel = ratio >= 0.9 ? '很相关' : ratio >= 0.6 ? '较相关' : '参考'
    }
    const isWebSource = source.source === 'web'
    return {
      id: String(source.chunk_id ?? `${isWebSource ? 'web' : 'source'}-${index}`),
      title: source.document_title || source.section_title || '未命名文档',
      type: isWebSource
        ? `联网资料${source.site_name ? ` · ${source.site_name}` : ''}`
        : source.page_number
          ? `知识库文档 · 第 ${source.page_number} 页`
          : '知识库文档',
      score: norm === null ? '—' : `${Math.round(norm * 100)}%`,
      relevanceLabel,
      color: sourceColors[index % sourceColors.length],
      content: source.content,
      documentId: source.document_id,
      chunkId: source.chunk_id,
      chunkIndex: source.chunk_index,
      pageNumber: source.page_number,
      sectionTitle: source.section_title,
      parentTitle: source.parent_title,
      kind: isWebSource ? 'web' : 'local',
      url: source.url,
      siteName: source.site_name,
      // 合并来源只占 1 条名额（读几条=展示几条=top_k），但模型读到的是 N 段拼接正文：
      // 用“含 N 段”把这个差别显式告知，而不是拆成 N 条来源。
      mergedParts: source.merged_parts ?? null,
    }
  })
}
