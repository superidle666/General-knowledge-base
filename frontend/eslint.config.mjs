/**
 * ESLint 配置：vue3 + TS 推荐规则 + 120 列行长上限。
 *
 * 生效范围：`npm run lint` 检查全量 src（2026-09-27 起，原豁免文件已完成
 * P2 令牌化与 Prettier 格式化）。单独检查某文件：npx eslint src/pages/user/index.vue
 * 如需临时豁免某文件，必须在上方 ignores 中登记原因与退出条件。
 */
import pluginVue from 'eslint-plugin-vue'
import tseslint from 'typescript-eslint'

export default tseslint.config(
  {
    // 不检查构建产物与依赖目录
    ignores: ['dist/**', 'node_modules/**'],
  },
  {
    // 存量豁免名单：原 P2 待重构文件已于 2026-09-27 完成令牌化并移除豁免；
    // 后续如需临时豁免，必须在此登记原因和退出条件。
    ignores: [],
  },
  ...tseslint.configs.recommended,
  ...pluginVue.configs['flat/recommended'],
  {
    files: ['**/*.vue'],
    languageOptions: {
      parserOptions: {
        parser: tseslint.parser,
      },
    },
    rules: {
      // vue 推荐集里的格式化类规则全部交给 Prettier，避免双重标准互相打架
      'vue/max-attributes-per-line': 'off',
      'vue/singleline-html-element-content-newline': 'off',
      'vue/multiline-html-element-content-newline': 'off',
      'vue/html-self-closing': 'off',
      'vue/html-indent': 'off',
      'vue/html-closing-bracket-newline': 'off',
      'vue/html-closing-bracket-spacing': 'off',
      'vue/attributes-order': 'off',
    },
  },
  {
    // 路由页面/布局按通行约定使用 index.vue，不受多词组件名限制；
    // src/components 下的可复用组件仍强制多词命名。
    files: ['src/pages/**/*.vue', 'src/layouts/**/*.vue'],
    rules: {
      'vue/multi-word-component-names': 'off',
    },
  },
  {
    rules: {
      // 行长硬约束：>120 列报错（存量违规文件已在上方豁免）
      'max-len': ['error', { code: 120, ignoreUrls: true, ignoreStrings: true, ignoreTemplateLiterals: false }],
      // TS 未使用变量：下划线前缀显式豁免
      '@typescript-eslint/no-unused-vars': ['error', { argsIgnorePattern: '^_', varsIgnorePattern: '^_' }],
    },
  },
)
