/** 操作确认对话框：把「先说清代价、再让用户点头」的交互收敛成一个口径。 */
import { DialogPlugin } from 'tdesign-vue-next'

export interface ConfirmDialogOptions {
  /** 标题写动作名（「删除文档」），不要写「提示」「确认」这类空标题。 */
  header: string
  /** 正文说清后果；不可逆或会影响在线回答的，必须在这句里讲明白。 */
  body: string
  /** 确认按钮文案，默认「确定」；破坏性动作用动词更不容易误点（「删除」）。 */
  confirmText?: string
  /** 确认按钮是否走危险配色。 */
  danger?: boolean
}

/**
 * 弹出确认对话框，返回用户是否确认。
 *
 * 选 DialogPlugin 而不是在页面里挂 `t-dialog`：确认点大多落在表格行上，甚至落在已打开的弹窗内部
 * （导入队列弹窗里还要再确认「删除任务记录」），编程式对话框挂在 body 上，不会和父弹窗互相压层级，
 * 也省掉每个页面一份 pending 状态 + computed 文案 + 模板。
 * 用户点取消、点右上角关闭、按 ESC 都算未确认。
 */
export function confirmDialog(options: ConfirmDialogOptions): Promise<boolean> {
  return new Promise((resolve) => {
    // 点取消会先后触发 onCancel 与 onClose，destroy() 只能调一次，第二次会报错。
    let settled = false
    const finish = (confirmed: boolean) => {
      if (settled) return
      settled = true
      dialog.destroy()
      resolve(confirmed)
    }
    const dialog = DialogPlugin.confirm({
      header: options.header,
      body: options.body,
      theme: 'warning',
      cancelBtn: '取消',
      confirmBtn: { content: options.confirmText ?? '确定', theme: options.danger ? 'danger' : 'primary' },
      onConfirm: () => finish(true),
      onCancel: () => finish(false),
      onClose: () => finish(false),
    })
  })
}
