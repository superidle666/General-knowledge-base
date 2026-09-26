/** 用户端页面、会话、消息、知识库和来源数据结构。 */

import type { QuerySource } from '../api/query'

export type UserMessage = {
  id: string
  role: 'user' | 'assistant'
  content: string
  time: string
  timestamp?: number
  streaming?: boolean
  imageUrls?: string[]
}

export type UserConversation = {
  id: string
  libraryId?: number
  title: string
  time: string
  group?: string
  active?: boolean
}

export type QuerySession = {
  id: string
  library_id: number
  title: string
  created_at: number
  updated_at: number
}

export type QueryChatMessage = {
  id: string
  role: 'user' | 'assistant'
  content: string
  created_at: number
  sources: QuerySource[]
  image_urls: string[]
}

export type QuerySessionMessages = {
  session_id: string
  library_id: number
  messages: QueryChatMessage[]
}

export type UserLibrary = {
  id: number
  name: string
  count: number
  color: string
  status: string
  updatedAt: string | null
}

export type UserSource = {
  id: string
  title: string
  type: string
  // 原始检索相关度（rerank 分转的百分比），仅作详情里的次级参考；降级态为“—”
  score: string
  // 检索相关性分档（很相关/较相关/参考/参考序），表达相对强弱而非答案置信度
  relevanceLabel: string
  color: string
  content: string
  documentId: number | null
  chunkId: string | number | null
  chunkIndex: number | null
  pageNumber: number | null
  sectionTitle: string | null
  parentTitle: string | null
  // 联网来源没有库内 ID，只能靠 url 溯源；缺省按本地知识库来源处理
  kind?: 'local' | 'web'
  url?: string | null
  siteName?: string | null
}