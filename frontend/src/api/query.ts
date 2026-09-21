/** 用户端知识库列表、连续会话、历史消息和流式问答接口。 */

import request from '../utils/request'
import { authenticatedFetch } from './auth'

export type QueryRequest = {
  library_id: number
  query: string
  session_id?: string
  top_k?: number
  use_hyde?: boolean
  use_web_search?: boolean
}

export type QuerySource = {
  chunk_id: string | number | null
  document_id: number | null
  document_title: string | null
  chunk_index: number | null
  page_number: number | null
  section_title: string | null
  parent_title: string | null
  content: string
  score: number | null
  source: string
}

export type QueryResponse = {
  library_id: number
  session_id: string | null
  query: string
  answer: string
  sources: QuerySource[]
  image_urls: string[]
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

export type QueryLibrary = {
  id: number
  name: string
  cover_color: string | null
  status: string
  document_count: number
  updated_at: string
}

export function listQueryLibraries() {
  return request<QueryLibrary[]>('/api/v1/query/libraries')
}

export function createQuerySession(libraryId: number, title = "新建技术咨询") {
  return request<QuerySession>('/api/v1/query/sessions', {
    method: 'POST',
    body: JSON.stringify({ library_id: libraryId, title }),
  })
}
export function listQuerySessions(libraryId: number) {
  return request<QuerySession[]>(`/api/v1/query/sessions?library_id=${encodeURIComponent(libraryId)}`)
}

export function getQuerySessionMessages(sessionId: string) {
  return request<QuerySessionMessages>(`/api/v1/query/sessions/${encodeURIComponent(sessionId)}/messages`)
}

export function renameQuerySession(sessionId: string, title: string) {
  return request<QuerySession>(`/api/v1/query/sessions/${encodeURIComponent(sessionId)}`, {
    method: 'PATCH',
    body: JSON.stringify({ title }),
  })
}

export function deleteQuerySession(sessionId: string) {
  return request<{ deleted: boolean }>(`/api/v1/query/sessions/${encodeURIComponent(sessionId)}`, {
    method: 'DELETE',
  })
}

export function queryKnowledgeBase(payload: QueryRequest, signal?: AbortSignal) {
  return request<QueryResponse>('/api/v1/query', {
    method: 'POST',
    body: JSON.stringify(payload),
    signal,
  })
}

type QueryStreamCallbacks = {
  onDelta?: (delta: string) => void
}

function getErrorMessage(body: unknown, fallback: string) {
  if (!body || typeof body !== 'object' || !('detail' in body)) return fallback
  return typeof body.detail === 'string' ? body.detail : fallback
}

function parseSseEvent(block: string) {
  const lines = block.split(/\r?\n/)
  const event = lines.find((line) => line.startsWith('event:'))?.slice(6).trim() || 'message'
  const data = lines
    .filter((line) => line.startsWith('data:'))
    .map((line) => line.slice(5).trimStart())
    .join('\n')
  return { event, data }
}

export async function queryKnowledgeBaseStream(
  payload: QueryRequest,
  callbacks: QueryStreamCallbacks = {},
  signal?: AbortSignal,
): Promise<QueryResponse> {
  let response: Response
  try {
    response = await authenticatedFetch('/api/v1/query/stream', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', Accept: 'text/event-stream' },
      body: JSON.stringify(payload),
      signal,
    })
  } catch {
    throw new Error('无法连接后端服务，请确认 FastAPI 服务已启动')
  }

  if (!response.ok) {
    const body = await response.json().catch(() => null)
    throw new Error(getErrorMessage(body, `请求失败（${response.status}）`))
  }
  if (!response.body) throw new Error('后端未返回流式响应')

  const reader = response.body.getReader()
  const decoder = new TextDecoder('utf-8')
  let buffer = ''
  let finalResult: QueryResponse | null = null

  function consume(block: string) {
    if (!block.trim()) return
    const { event, data } = parseSseEvent(block)
    let parsed: Record<string, unknown> = {}
    try {
      parsed = JSON.parse(data) as Record<string, unknown>
    } catch {
      throw new Error('后端返回了无法解析的流式数据')
    }
    if (event === 'delta') {
      callbacks.onDelta?.(typeof parsed.delta === 'string' ? parsed.delta : '')
      return
    }
    if (event === 'error') throw new Error(typeof parsed.error === 'string' ? parsed.error : '查询失败，请稍后重试')
    if (event === 'final') finalResult = parsed as unknown as QueryResponse
  }

  try {
    while (true) {
      const { value, done } = await reader.read()
      buffer += decoder.decode(value || new Uint8Array(), { stream: !done })
      const events = buffer.split(/\r?\n\r?\n/)
      buffer = events.pop() || ''
      events.forEach(consume)
      if (done) break
    }
    if (buffer.trim()) consume(buffer)
  } finally {
    reader.releaseLock()
  }

  if (!finalResult) throw new Error('流式响应已结束，但未收到最终结果')
  return finalResult
}
