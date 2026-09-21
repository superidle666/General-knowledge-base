/** localStorage 安全读写工具，兼容不可用存储环境。 */

export function set<T>(key: string, value: T): void {
  try {
    localStorage.setItem(key, JSON.stringify(value))
  } catch {
    // Ignore unavailable storage and serialization failures.
  }
}

export function get<T>(key: string, defaultValue: T | null = null): T | null {
  try {
    const value = localStorage.getItem(key)
    return value === null ? defaultValue : JSON.parse(value) as T
  } catch {
    return defaultValue
  }
}

export function remove(key: string): void {
  try {
    localStorage.removeItem(key)
  } catch {
    // Ignore unavailable storage failures.
  }
}

export function clear(): void {
  try {
    localStorage.clear()
  } catch {
    // Ignore unavailable storage failures.
  }
}