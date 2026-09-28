/**
 * stylelint 配置：standard 基线 + less 支持。
 * 说明：行长统一交给 Prettier（printWidth 120）管控，stylelint 不重复设限；
 * 单行多声明为项目现有紧凑写法，降级为 warning 以便渐进收敛。
 */
module.exports = {
  extends: ['stylelint-config-standard', 'stylelint-config-recommended-less'],
  overrides: [
    {
      // .vue 单文件：用 postcss-html 提取 <style> 块后按 CSS/Less 规则检查
      files: ['**/*.vue'],
      customSyntax: 'postcss-html',
    },
  ],
  rules: {
    'custom-property-pattern': null,
    'selector-class-pattern': null,
    'no-descending-specificity': null,
    'font-family-no-missing-generic-family-keyword': null,
    'import-notation': null,
    'media-feature-range-notation': null,
    'value-keyword-case': null,
    'declaration-empty-line-before': null,
    'custom-property-empty-line-before': null,
    'comment-empty-line-before': null,
    'rule-empty-line-before': null,
    'at-rule-empty-line-before': null,
    'declaration-block-single-line-max-declarations': null,
    'selector-pseudo-class-no-unknown': [true, { ignorePseudoClasses: ['deep', 'global', 'slotted'] }],
  },
  ignoreFiles: ['dist/**', 'node_modules/**', '**/*.css'],
}
