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
  score: string
  color: string
  content: string
  documentId: number | null
  chunkId: string | number | null
  chunkIndex: number | null
  pageNumber: number | null
  sectionTitle: string | null
  parentTitle: string | null
}