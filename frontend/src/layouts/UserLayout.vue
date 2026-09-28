<!--
 * @FilePath: frontend/src/layouts/UserLayout.vue
 * @Date: 2026-09-07
 * @Description: 用户端整体布局，提供侧栏、顶部导航、问答区域和响应式样式容器。
 * @BusinessRule: 颜色统一引用 style/tokens.less 语义令牌，禁止硬编码；暗色仅靠 .is-dark 切换变量。
 -->
<script setup lang="ts">
defineProps<{ darkMode?: boolean }>()
</script>

<template>
  <div class="user-shell" :class="{ 'is-dark': darkMode }">
    <slot />
  </div>
</template>

<style>
/* 颜色全部来自 style/tokens.less；本文件只保留布局尺寸与结构 */
.user-shell {
  display: flex;
  width: 100%;
  height: 100dvh;
  min-width: 0;
  overflow: hidden;
  background: var(--bg);
  color: var(--text);
  transition:
    background 0.2s,
    color 0.2s;
}
.user-shell button,
.user-shell textarea,
.user-shell input {
  font: inherit;
}
.user-shell .eyebrow,
.user-shell .nav-label {
  font-size: 11px;
  color: var(--weak);
  letter-spacing: 0.04em;
}

/* ===== 侧栏（对齐管理端 admin-sidebar） ===== */
.user-shell .sidebar {
  width: 232px;
  flex: 0 0 232px;
  background: var(--surface);
  border-right: 1px solid var(--border);
  display: flex;
  flex-direction: column;
  min-height: 0;
  overflow: hidden;
  transition:
    width 0.2s,
    transform 0.2s;
  z-index: 20;
}
.user-shell .brand-row {
  height: 70px;
  flex: 0 0 70px;
  padding: 0 18px;
  display: flex;
  align-items: center;
  gap: 10px;
  border-bottom: 1px solid var(--line-strong);
}
.user-shell .brand-mark {
  width: 38px;
  height: 38px;
  flex: 0 0 auto;
  display: grid;
  place-items: center;
  border-radius: 9px;
  color: var(--on-primary);
  font-weight: 700;
  font-size: 18px;
  background: var(--primary);
}
.user-shell .brand-copy {
  display: grid;
  gap: 2px;
  min-width: 0;
}
.user-shell .brand-copy strong {
  font-size: 15px;
}
.user-shell .brand-copy span {
  color: var(--muted);
  font-size: 10px;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.user-shell .sidebar-close {
  display: none;
  margin-left: auto;
  border: 0;
  background: transparent;
  color: var(--muted);
}
.user-shell .sidebar-collapse {
  margin-left: auto;
  width: 26px;
  height: 26px;
  flex: 0 0 auto;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  border: 0;
  border-radius: 5px;
  background: transparent;
  color: var(--muted);
  cursor: pointer;
}
.user-shell .sidebar-collapse:hover {
  color: var(--primary);
  background: var(--surface-soft);
}
.user-shell .collapse-icon {
  width: 17px;
  height: 17px;
}
.user-shell .sidebar-content {
  padding: 22px 12px;
  flex: 1;
  min-height: 0;
  overflow-y: auto;
  overscroll-behavior: contain;
  /* 始终预留滚动条槽位：会话/知识库多少导致滚动条出现或消失时，内容宽度不变，避免左右挤压跳动 */
  scrollbar-gutter: stable;
}
.user-shell .nav-label {
  padding: 0 12px 10px;
}
.user-shell .nav-item {
  width: 100%;
  height: 42px;
  display: flex;
  align-items: center;
  gap: 11px;
  border: 0;
  border-radius: 5px;
  background: transparent;
  color: var(--muted);
  padding: 0 12px;
  margin-bottom: 3px;
  text-align: left;
  font-size: 13px;
  position: relative;
}
.user-shell .nav-item:hover,
.user-shell .nav-item.active {
  color: var(--primary);
  background: var(--primary-weak);
}
.user-shell .nav-item.active {
  font-weight: 600;
}
.user-shell .nav-item svg {
  width: 17px;
  flex: 0 0 auto;
}
.user-shell .nav-count {
  margin-left: auto;
  color: var(--weak);
  font-size: 10px;
}
.user-shell .nav-chevron {
  /* 不再用 margin-left:auto（会与 nav-count 的 auto 平分空间、把总数挤到中间），改为固定小间距紧跟总数右侧 */
  margin-left: 6px;
  width: 14px;
  flex: 0 0 auto;
  transition: transform 0.18s ease;
}
.user-shell .nav-chevron.rotated {
  transform: rotate(180deg);
}
.user-shell .library-list {
  display: grid;
  gap: 2px;
  padding: 0 0 6px 30px;
}
.user-shell .library-item {
  width: 100%;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 7px;
  border: 0;
  border-left: 1px solid var(--line-strong);
  background: transparent;
  color: var(--muted);
  padding: 7px 10px;
  text-align: left;
  font-size: 11px;
}
.user-shell .library-item:hover,
.user-shell .library-item.selected {
  color: var(--primary);
  border-left-color: var(--primary);
  background: var(--primary-weak);
}
.user-shell .library-item .library-dot {
  width: 6px;
  height: 6px;
  flex: 0 0 auto;
}
.user-shell .library-item small {
  margin-left: auto;
  color: var(--weak);
  font-size: 10px;
}
.user-shell .library-more {
  width: 100%;
  padding: 5px 10px;
  border: 0;
  border-left: 1px solid transparent;
  background: transparent;
  color: var(--primary);
  text-align: left;
  font-size: 11px;
  cursor: pointer;
}
.user-shell .library-more:hover {
  text-decoration: underline;
}
.user-shell .library-dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: var(--primary);
  display: inline-block;
}
.user-shell .search-nav {
  height: 40px;
  display: flex;
  align-items: center;
  gap: 10px;
  margin-top: 14px;
  padding: 0 12px;
  border: 1px solid var(--border);
  border-radius: 6px;
  background: var(--surface);
  color: var(--muted);
}
.user-shell .search-nav:hover,
.user-shell .search-nav:focus-within {
  color: var(--muted);
  background: var(--surface);
}
.user-shell .search-nav input {
  flex: 1;
  min-width: 0;
  border: 0;
  outline: 0;
  background: transparent;
  color: var(--text);
  font-size: 12px;
}
.user-shell .search-close {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 22px;
  height: 22px;
  border: 0;
  border-radius: 4px;
  background: transparent;
  color: var(--muted);
  font-size: 16px;
  line-height: 1;
}
.user-shell .search-close:hover {
  background: var(--surface-soft);
  color: var(--text);
}
.user-shell .conversation-section {
  padding-top: 22px;
}
.user-shell .section-heading {
  display: flex;
  justify-content: space-between;
  align-items: center;
  color: var(--muted);
  font-size: 12px;
  margin-bottom: 10px;
  padding: 0 2px;
}
.user-shell .new-chat-button {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  min-width: 104px;
  min-height: 38px;
  padding: 0 18px;
  border: 1px solid var(--primary) !important;
  border-radius: 6px;
  background: var(--primary) !important;
  color: var(--on-primary) !important;
  font-size: 12px;
  font-weight: 500;
}
.user-shell .new-chat-button:hover,
.user-shell .new-chat-button:focus,
.user-shell .new-chat-button:active {
  border-color: var(--primary-deep) !important;
  background: var(--primary-deep) !important;
  color: var(--on-primary) !important;
}
.user-shell .new-chat-button .t-icon {
  margin-right: 6px;
  color: inherit;
  flex: 0 0 auto;
}
.user-shell .conversation-list {
  display: grid;
  gap: 3px;
}
.user-shell .history-group {
  display: grid;
  gap: 3px;
}
.user-shell .history-group + .history-group {
  margin-top: 12px;
}
.user-shell .history-group-title {
  display: flex;
  align-items: center;
  justify-content: space-between;
  /* 多选按钮靠最右边（与会话行 more 按钮右缘对齐） */
  padding: 0 6px 4px 8px;
  color: var(--weak);
  font-size: 10px;
  font-weight: 600;
  letter-spacing: 0.04em;
}
.user-shell .history-group-title .select-mode-button {
  width: 22px;
  height: 22px;
  margin-top: -4px;
}
.user-shell .conversation-row {
  position: relative;
  display: flex;
  align-items: center;
  min-width: 0;
  gap: 0;
}
.user-shell .conversation-item {
  flex: 1;
  min-width: 0;
  width: 100%;
  min-height: 42px;
  border: 0;
  display: grid;
  grid-template-columns: 7px 1fr auto;
  align-items: center;
  gap: 8px;
  background: transparent;
  padding: 7px 8px;
  padding-right: 34px;
  border-radius: 6px;
  color: var(--muted);
  text-align: left;
}
.user-shell .conversation-item:hover,
.user-shell .conversation-item.selected {
  background: var(--surface-soft);
  color: var(--text);
}
.user-shell .conversation-dot {
  width: 5px;
  height: 5px;
  border-radius: 50%;
  background: var(--weak);
}
.user-shell .conversation-item.selected .conversation-dot {
  background: var(--primary);
}
.user-shell .conversation-title {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-size: 12px;
}
.user-shell .conversation-time {
  font-size: 10px;
  color: var(--weak);
}
.user-shell .conversation-more {
  position: absolute;
  right: 6px;
  top: 50%;
  display: none;
  align-items: center;
  justify-content: center;
  width: 26px;
  height: 26px;
  transform: translateY(-50%);
  border: 1px solid var(--border);
  border-radius: 50%;
  background: var(--surface);
  color: var(--muted);
}
.user-shell .conversation-row:hover .conversation-more,
.user-shell .conversation-more:focus-visible {
  display: inline-flex;
}
.user-shell .conversation-more:hover {
  border-color: var(--primary);
  background: var(--surface-soft);
  color: var(--primary);
}
.user-shell .conversation-menu {
  position: absolute;
  z-index: 10;
  top: calc(50% + 16px);
  right: 6px;
  display: grid;
  width: 88px;
  padding: 4px;
  border: 1px solid var(--border);
  border-radius: 6px;
  background: var(--surface);
  box-shadow: var(--shadow-pop);
}
.user-shell .conversation-menu button {
  height: 30px;
  border: 0;
  border-radius: 4px;
  background: transparent;
  color: var(--text);
  text-align: left;
  padding: 0 9px;
  font-size: 12px;
}
.user-shell .conversation-menu button:hover {
  background: var(--surface-soft);
}
.user-shell .conversation-menu .danger {
  color: var(--danger);
}
.user-shell .conversation-empty {
  padding: 12px 8px;
  color: var(--muted);
  font-size: 11px;
  text-align: center;
}
.user-shell .select-mode-button {
  width: 26px;
  height: 26px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  border: 1px solid var(--border);
  border-radius: 6px;
  background: var(--surface);
  color: var(--muted);
  cursor: pointer;
  transition:
    color 0.15s,
    border-color 0.15s,
    background 0.15s;
}
.user-shell .select-mode-button:hover,
.user-shell .select-mode-button.active {
  color: var(--primary);
  border-color: var(--primary);
  background: var(--primary-weak);
}
.user-shell .selection-bar {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 5px 8px;
  margin-bottom: 8px;
  border: 1px solid var(--border);
  border-radius: 6px;
  background: var(--surface-soft);
  font-size: 11px;
  white-space: nowrap;
}
.user-shell .selection-all {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  cursor: pointer;
  color: var(--text);
  font-size: 11px;
  white-space: nowrap;
}
.user-shell .selection-count {
  color: var(--muted);
  margin-left: auto;
  font-size: 11px;
  white-space: nowrap;
}
.user-shell .selection-cancel,
.user-shell .selection-delete {
  height: 22px;
  flex: 0 0 auto;
  padding: 0 8px;
  border: 1px solid var(--border);
  border-radius: 5px;
  background: var(--surface);
  color: var(--text);
  font-size: 11px;
  white-space: nowrap;
  cursor: pointer;
}
.user-shell .selection-cancel:hover {
  border-color: var(--primary);
  color: var(--primary);
}
.user-shell .selection-delete {
  border-color: var(--danger);
  color: var(--danger);
}
.user-shell .selection-delete:hover:not(:disabled) {
  background: var(--danger);
  color: var(--on-primary);
}
.user-shell .selection-delete:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}
.user-shell .conversation-row.is-selecting .conversation-item {
  grid-template-columns: 18px 1fr auto;
  padding-right: 8px;
}
.user-shell .conversation-check {
  width: 16px;
  height: 16px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  border: 1px solid var(--border);
  border-radius: 4px;
  background: var(--surface);
  color: var(--on-primary);
}
.user-shell .conversation-check svg {
  width: 12px;
}
.user-shell .conversation-row.is-checked .conversation-check {
  border-color: var(--primary);
  background: var(--primary);
}
.user-shell .conversation-row.is-checked .conversation-item {
  background: var(--primary-weak);
  color: var(--text);
}
.user-shell .sidebar-footer {
  border-top: 1px solid var(--line-strong);
  padding: 14px 12px;
  flex-shrink: 0;
}
.user-shell .user-card {
  display: flex;
  align-items: center;
  gap: 9px;
  min-width: 0;
}
.user-shell .user-copy {
  display: grid;
  gap: 2px;
  min-width: 0;
  flex: 1;
}
.user-shell .user-copy strong {
  font-size: 12px;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.user-shell .user-copy span {
  color: var(--weak);
  font-size: 10px;
}
.user-shell .avatar {
  width: 32px;
  height: 32px;
  border-radius: 50%;
  display: grid;
  place-items: center;
  background: var(--primary-soft);
  color: var(--primary-deep);
  font-size: 13px;
  font-weight: 700;
  flex: 0 0 auto;
}
.user-shell .avatar.small {
  width: 30px;
  height: 30px;
}

/* ===== 工作区与顶栏（对齐管理端 admin-topbar） ===== */
.user-shell .workspace {
  min-width: 0;
  flex: 1;
  width: 0;
  position: relative;
  display: flex;
  flex-direction: column;
  height: 100%;
  overflow: hidden;
}
.user-shell .topbar {
  /* 与侧栏品牌区 70px 等高，保证两条下边框对齐（同管理端口径） */
  height: 70px;
  flex: 0 0 70px;
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 10px;
  padding: 0 30px;
  border-bottom: 1px solid var(--border);
  background: var(--surface);
  overflow: hidden;
}
.user-shell .topbar-left,
.user-shell .topbar-right {
  display: flex;
  align-items: center;
  gap: 10px;
  min-width: 0;
}
.user-shell .topbar-left svg {
  width: 15px;
  color: var(--weak);
}
.user-shell .topbar-crumb {
  color: var(--muted);
  font-size: 12px;
  white-space: nowrap;
}
.user-shell .topbar-crumb-sep {
  flex: 0 0 auto;
}
.user-shell .topbar-title {
  font-size: 13px;
  font-weight: 700;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.user-shell .mock-badge {
  display: flex;
  align-items: center;
  gap: 6px;
  color: var(--success);
  font-size: 11px;
  /* 与管理端已认证徽标同款：实底浅绿背景 */
  background: var(--success-bg);
  border: 1px solid var(--success-border);
  border-radius: 4px;
  padding: 4px 8px;
  white-space: nowrap;
}
.user-shell .mock-badge.is-idle {
  color: var(--muted);
  background: transparent;
  border-color: var(--border);
}
.user-shell .mock-badge.is-idle .pulse-dot {
  background: var(--weak);
}
.user-shell .pulse-dot {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: var(--success-strong);
  flex: 0 0 auto;
}
.user-shell .topbar-account {
  display: grid;
  gap: 2px;
  text-align: right;
  min-width: 0;
}
.user-shell .topbar-account strong {
  font-size: 12px;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  max-width: 130px;
}
.user-shell .topbar-account span {
  color: var(--weak);
  font-size: 10px;
}
.user-shell .user-top-theme {
  color: var(--muted);
}
.user-shell .user-top-theme:hover {
  color: var(--primary);
  background: var(--primary-weak);
}
.user-shell .user-top-logout {
  color: var(--muted);
}
.user-shell .user-top-logout:hover {
  color: var(--danger);
  background: var(--danger-bg);
}
.user-shell .user-top-logout .t-icon,
.user-shell .user-top-theme .t-icon {
  width: 17px;
  height: 17px;
  flex: 0 0 auto;
}
.user-shell .mobile-menu {
  display: none;
}

/* ===== 问答内容区 ===== */
.user-shell .content-grid {
  min-height: 0;
  flex: 1;
  display: grid;
  grid-template-columns: minmax(0, 1fr);
  overflow: hidden;
}
.user-shell .content-grid.has-sources {
  grid-template-columns: minmax(0, 1fr) 320px;
}
.user-shell .chat-panel {
  /* 父定位上下文：空会话时标题以浮层方式叠在左上角；composer 回正常流居底，滚动区止于其顶边 */
  position: relative;
  min-width: 0;
  min-height: 0;
  display: flex;
  flex-direction: column;
  overflow: hidden;
  background: var(--bg);
}
.user-shell .chat-head {
  /* 标题以浮层方式放在左上角：不占据独立行，也不加下边框 */
  position: absolute;
  z-index: 2;
  top: 10px;
  left: 30px;
  right: 30px;
  display: flex;
  align-items: center;
  pointer-events: none;
}
.user-shell .chat-head-title {
  pointer-events: auto;
  background: var(--bg);
  padding: 2px 8px;
  border-radius: 4px;
  max-width: 100%;
  margin: 0;
  font-size: 14px;
  font-weight: 600;
  line-height: 1.4;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  min-width: 0;
}
.user-shell .chat-head h1 {
  pointer-events: auto;
  background: var(--bg);
  padding: 2px 8px;
  border-radius: 4px;
  max-width: 100%;
  margin: 0;
  font-size: 14px;
  font-weight: 600;
  line-height: 1.4;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  min-width: 0;
}
.user-shell .chat-scroll {
  /* 滚动区在正常 flex 列中占据 chat-head 与 composer 之间的空间，消息滑到 composer 顶边即被裁切 */
  flex: 1;
  min-height: 0;
  overflow-y: auto;
  overscroll-behavior: contain;
  padding: 18px 30px 24px;
}
.user-shell .message {
  display: flex;
  gap: 12px;
  max-width: 860px;
  margin: 0 auto 28px;
}
.user-shell .message-avatar {
  width: 30px;
  height: 30px;
  border-radius: 7px;
  display: grid;
  place-items: center;
  flex: 0 0 auto;
  font-size: 13px;
  font-weight: 700;
}
.user-shell .message-avatar.user {
  background: var(--primary-soft);
  color: var(--primary-deep);
}
.user-shell .message-avatar.assistant {
  background: var(--primary);
  color: var(--on-primary);
}
.user-shell .message-body {
  min-width: 0;
  flex: 1;
}
.user-shell .message-meta {
  min-height: 22px;
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 12px;
  color: var(--muted);
}
.user-shell .message-meta strong {
  color: var(--text);
  font-size: 13px;
}
.user-shell .source-tag {
  color: var(--primary);
  background: var(--primary-weak);
  padding: 2px 6px;
  border-radius: 3px;
  font-size: 10px;
  cursor: pointer;
  transition:
    background 0.15s,
    color 0.15s;
}
.user-shell .source-tag:hover,
.user-shell .source-tag:focus-visible {
  color: var(--on-primary);
  background: var(--primary);
}
.user-shell .user-text {
  display: inline-block;
  margin-top: 6px;
  padding: 11px 14px;
  border-radius: 4px 12px 12px;
  background: var(--surface);
  border: 1px solid var(--border);
  font-size: 14px;
  line-height: 1.6;
  overflow-wrap: anywhere;
}
.user-shell .markdown-content {
  margin-top: 5px;
  color: var(--text);
  font-size: 14px;
  line-height: 1.75;
  overflow-wrap: anywhere;
}
.user-shell .markdown-content p {
  margin: 0 0 12px;
}
.user-shell .markdown-content ol,
.user-shell .markdown-content ul {
  margin: 8px 0 12px;
  padding-left: 22px;
}
.user-shell .markdown-content li {
  margin: 4px 0;
}
.user-shell .markdown-content strong {
  color: var(--primary);
}
.user-shell .message-tools {
  display: flex;
  gap: 2px;
  margin-top: 8px;
}
.user-shell .message-tools .t-button {
  display: inline-flex;
  align-items: center;
  color: var(--muted);
  padding: 0 6px;
}
.user-shell .message-tools .t-icon {
  flex: 0 0 auto;
}
.user-shell .typing-indicator {
  display: flex;
  gap: 3px;
  margin-top: 8px;
}
.user-shell .typing-indicator i {
  width: 5px;
  height: 5px;
  border-radius: 50%;
  background: var(--primary);
  animation: user-blink 1s infinite alternate;
}
.user-shell .typing-indicator i:nth-child(2) {
  animation-delay: 0.2s;
}
.user-shell .typing-indicator i:nth-child(3) {
  animation-delay: 0.4s;
}
@keyframes user-blink {
  from {
    opacity: 0.25;
    transform: translateY(0);
  }
  to {
    opacity: 1;
    transform: translateY(-2px);
  }
}
.user-shell .suggestions {
  max-width: 860px;
  margin: 0 auto;
  border-top: 1px solid var(--border);
  padding-top: 16px;
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
}
.user-shell .suggestions > span {
  width: 100%;
  color: var(--muted);
  font-size: 11px;
}
.user-shell .suggestions button {
  border: 1px solid var(--border);
  color: var(--muted);
  background: var(--surface);
  padding: 7px 10px;
  border-radius: 4px;
  font-size: 12px;
}
.user-shell .suggestions button:hover {
  color: var(--primary);
  border-color: var(--primary);
}
.user-shell .composer-wrap {
  /* 正常流居底：不遮挡滚动区，消息在滚动区底边（即输入框上沿）处被裁切消失 */
  flex: 0 0 auto;
  max-width: 860px;
  width: calc(100% - 96px);
  box-sizing: border-box;
  margin: 0 auto;
  padding-bottom: 12px;
}
.user-shell .composer {
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 10px 12px 8px;
  box-shadow: var(--shadow-soft);
}
.user-shell .composer textarea {
  width: 100%;
  min-width: 0;
  box-sizing: border-box;
  min-height: 30px;
  /* 必须与 UserChatPanel 的 INPUT_MAX_HEIGHT(200) 一致：否则内容在 120~200px 区间被 CSS 夹住、JS 又未开滚动条，导致看不到前几行 */
  max-height: 200px;
  resize: none;
  border: 0;
  outline: 0;
  background: transparent;
  color: var(--text);
  font-size: 14px;
  line-height: 1.5;
  overflow-y: hidden;
}
.user-shell .composer textarea::placeholder {
  color: var(--weak);
}
.user-shell .composer-bottom {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  margin-top: 8px;
}
.user-shell .composer-bottom .t-button {
  display: inline-flex;
  align-items: center;
  justify-content: center;
}
.user-shell .composer-bottom .t-icon {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  flex: 0 0 auto;
}
.user-shell .composer-hints {
  display: flex;
  align-items: center;
  gap: 12px;
  min-width: 0;
}
.user-shell .hint-text {
  color: var(--weak);
  font-size: 11px;
}
.user-shell .privacy-note {
  color: var(--weak);
  text-align: center;
  font-size: 10px;
  margin-top: 10px;
}
.user-shell .send-icon-button {
  width: 34px;
  min-width: 34px;
  height: 34px;
  padding: 0;
}
.user-shell .send-icon-button .t-icon {
  margin: 0;
}

/* ===== 来源面板 ===== */
.user-shell .source-panel {
  min-width: 0;
  min-height: 0;
  display: flex;
  flex-direction: column;
  overflow: hidden;
  border-left: 1px solid var(--border);
  background: var(--surface);
  padding: 28px 22px;
}
.user-shell .source-head {
  flex: 0 0 auto;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  /* 与下方来源列表拉开间距，头部下边缘不紧贴来源模块 */
  margin-bottom: 18px;
}
.user-shell .source-head-title {
  flex: 0 0 auto;
}
.user-shell .source-head h2 {
  margin: 5px 0 0;
  font-size: 18px;
}
.user-shell .source-head .t-button {
  display: inline-flex;
  align-items: center;
  justify-content: center;
}
.user-shell .source-head .t-icon {
  flex: 0 0 auto;
}
.user-shell .source-summary {
  flex: 1 1 auto;
  min-width: 0;
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 9px;
  background: var(--surface-soft);
  padding: 8px 10px;
  border-radius: 6px;
}
.user-shell .summary-icon {
  width: 20px;
  height: 20px;
  display: grid;
  place-items: center;
  background: var(--success-weak);
  color: var(--success);
  border-radius: 50%;
  font-size: 12px;
}
.user-shell .source-summary strong {
  font-size: 12px;
}
.user-shell .source-list {
  display: grid;
  gap: 10px;
  min-height: 0;
  flex: 1 1 auto;
  overflow-y: auto;
  overscroll-behavior: contain;
  padding-right: 2px;
}
.user-shell .source-card {
  border: 1px solid var(--border);
  border-radius: 6px;
  padding: 12px;
  min-width: 0;
}
.user-shell .source-card-top {
  display: flex;
  justify-content: space-between;
  align-items: center;
}
.user-shell .file-icon {
  width: 28px;
  height: 28px;
  display: grid;
  place-items: center;
  border-radius: 5px;
}
.user-shell .file-icon svg {
  width: 16px;
}
.user-shell .score {
  font-size: 11px;
  color: var(--success);
  font-weight: 600;
}
.user-shell .source-card h3 {
  font-size: 13px;
  margin: 10px 0 4px;
  overflow-wrap: anywhere;
}
.user-shell .source-card p {
  font-size: 11px;
  color: var(--muted);
  margin: 0;
  overflow-wrap: anywhere;
}
.user-shell .source-card > button {
  display: inline-flex;
  align-items: center;
  gap: 3px;
  border: 0;
  background: transparent;
  color: var(--primary);
  padding: 9px 0 0;
  font-size: 11px;
}
.user-shell .source-card > button svg {
  width: 12px;
  flex: 0 0 auto;
}
.user-shell .online-section {
  flex: 0 0 auto;
  margin-top: 26px;
}
.user-shell .optional {
  color: var(--muted);
  font-size: 11px;
}
.user-shell .online-empty {
  border: 1px dashed var(--border);
  color: var(--muted);
  padding: 14px 10px;
  text-align: center;
  border-radius: 5px;
}
.user-shell .online-empty svg {
  width: 17px;
}
.user-shell .online-empty p {
  margin: 5px 0 0;
  font-size: 11px;
}
.user-shell .empty-chat {
  max-width: 500px;
  margin: 110px auto;
  text-align: center;
  color: var(--muted);
}
.user-shell .empty-icon {
  width: 48px;
  height: 48px;
  margin: 0 auto 14px;
  border-radius: 11px;
  display: grid;
  place-items: center;
  background: var(--primary-soft);
  color: var(--primary);
  font-size: 20px;
  font-weight: 700;
}
.user-shell .empty-chat h2 {
  color: var(--text);
  font-size: 19px;
  margin: 0 0 8px;
}
.user-shell .empty-chat p {
  margin: 0;
  font-size: 13px;
}
.user-shell .mobile-backdrop {
  display: none;
}
.user-shell .user-query-error {
  margin: 0;
  padding: 10px 30px;
  color: var(--danger-deep);
  background: var(--danger-bg);
  border-top: 1px solid var(--danger-border);
  font-size: 12px;
}

/* ===== 侧栏收起态（仅桌面端，>=821px；移动端为抽屉不受影响） ===== */
@media (min-width: 821px) {
  .user-shell .sidebar.is-collapsed {
    width: 88px;
    flex-basis: 88px;
    /* 收起态与对话区融为一体：去掉右侧竖线，背景对齐对话区（var(--bg)） */
    border-right: none;
    background: var(--bg);
  }
  .user-shell .sidebar.is-collapsed .brand-row {
    /* 左右间距均等：logo 与展开按钮之间、两侧到边缘的距离全部相等（space-evenly），不再顶到右边缘被裁 */
    padding: 0;
    gap: 0;
    justify-content: space-evenly;
  }
  .user-shell .sidebar.is-collapsed .brand-copy {
    display: none;
  }
  .user-shell .sidebar.is-collapsed .sidebar-collapse {
    margin-left: 0;
  }
  /* 收起态：品牌行以下（知识库导航/会话列表/登录卡片）整体隐藏，该区域完全留白 */
  .user-shell .sidebar.is-collapsed .sidebar-content,
  .user-shell .sidebar.is-collapsed .sidebar-footer {
    display: none;
  }
}

/* ===== 响应式（对齐管理端 1100px / 820px / 520px 断点行为） ===== */
@media (max-width: 1100px) {
  .user-shell .content-grid,
  .user-shell .content-grid.has-sources {
    grid-template-columns: minmax(0, 1fr);
  }
  .user-shell .source-panel {
    display: none;
  }
  .user-shell .chat-head,
  .user-shell .chat-scroll {
    padding-left: 30px;
    padding-right: 30px;
  }
  .user-shell .chat-head {
    left: 30px;
    right: 30px;
  }
  .user-shell .composer-wrap {
    width: calc(100% - 60px);
  }
}
@media (max-width: 820px) {
  .user-shell .sidebar-collapse {
    display: none;
  }
  .user-shell .sidebar {
    position: fixed;
    inset: 0 auto 0 0;
    transform: translateX(-100%);
    box-shadow: var(--shadow-side);
  }
  .user-shell .sidebar.is-mobile-open {
    transform: translateX(0);
  }
  .user-shell .sidebar-close,
  .user-shell .mobile-menu {
    display: inline-flex;
  }
  .user-shell .mobile-backdrop {
    display: block;
    position: fixed;
    inset: 0;
    background: var(--overlay);
    z-index: 10;
  }
  .user-shell .topbar {
    padding: 0 16px;
  }
  .user-shell .content-grid {
    grid-template-columns: minmax(0, 1fr);
  }
  .user-shell .source-panel {
    display: none;
  }
}
@media (max-width: 520px) {
  /* 顶栏不再降级到 56px：与管理端一致全断点保持 70px（侧栏此时为浮层抽屉，不存在对齐问题） */
  .user-shell .mock-badge {
    display: none;
  }
  .user-shell .chat-head {
    left: 16px;
    right: 16px;
  }
  .user-shell .chat-scroll {
    padding-left: 16px;
    padding-right: 16px;
  }
  .user-shell .chat-head h1 {
    font-size: 14px;
  }
  .user-shell .message {
    gap: 8px;
    margin-bottom: 22px;
  }
  .user-shell .message-avatar {
    width: 26px;
    height: 26px;
  }
  .user-shell .message-meta {
    font-size: 11px;
  }
  .user-shell .markdown-content,
  .user-shell .user-text {
    font-size: 13px;
  }
  .user-shell .composer-hints .hint-text {
    display: none;
  }
  .user-shell .suggestions button {
    width: 100%;
    text-align: left;
  }
  .user-shell .composer-bottom .t-button {
    height: 32px;
  }
  .user-shell .empty-chat {
    margin: 75px auto;
  }
  .user-shell .composer-wrap {
    width: calc(100% - 32px);
  }
}
</style>
