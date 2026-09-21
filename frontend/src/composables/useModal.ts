/** 通用弹窗状态与打开、关闭、确认回调管理。 */

import { ref } from 'vue'

export type UseModalOptions = {
  onOpen?: () => void | Promise<void>
  onClose?: () => void | Promise<void>
  onConfirm?: () => void | Promise<void>
  onCancel?: () => void | Promise<void>
}

export function useModal(options: UseModalOptions = {}) {
  const visible = ref(false)

  async function open() {
    visible.value = true
    await options.onOpen?.()
  }

  async function close() {
    visible.value = false
    await options.onClose?.()
  }

  async function toggle() {
    if (visible.value) await close()
    else await open()
  }

  async function confirm() {
    await options.onConfirm?.()
    await close()
  }

  async function cancel() {
    await options.onCancel?.()
    await close()
  }

  return { visible, open, close, toggle, confirm, cancel }
}