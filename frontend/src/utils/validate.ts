/** 用户输入校验工具。 */

import { REGEX } from '../constants'

export function isEmail(value: string): boolean {
  return REGEX.EMAIL.test(value.trim())
}

export function isPhone(value: string): boolean {
  return REGEX.PHONE.test(value.trim())
}

export function isURL(value: string): boolean {
  try {
    const url = new URL(value.trim())
    return url.protocol === 'http:' || url.protocol === 'https:'
  } catch {
    return false
  }
}

export function isEmpty(value: unknown): boolean {
  if (value === null || value === undefined) return true
  if (typeof value === 'string') return value.trim().length === 0
  if (Array.isArray(value)) return value.length === 0
  if (typeof value === 'object') return Object.keys(value).length === 0
  return false
}