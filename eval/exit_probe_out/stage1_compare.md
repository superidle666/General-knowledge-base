
### 37 单款库（L1_single_model，12 条）

| 出口 | 改造前基线 | 阶段一严格档 |
| --- | --- | --- |
| `hit_exact` | 5 | 5 |
| `single_candidate` | 2 | 2 |
| `single_product_narrowed` | 0 | 2 |
| `brand_narrowed` | 1 | 1 |
| `no_brand_group` | 2 | 1 |
| `refuse` | 1 | 1 |
| `no_mention` | 1 | 0 |

### 38 跨品牌两款（L2_two_brands，12 条）

| 出口 | 改造前基线 | 阶段一严格档 |
| --- | --- | --- |
| `confirm_clarify` | 0 | 3 |
| `clarify` | 2 | 2 |
| `hit_exact` | 2 | 2 |
| `no_brand_group` | 3 | 2 |
| `brand_narrowed` | 1 | 1 |
| `single_candidate` | 1 | 1 |
| `unlisted_brand` | 0 | 1 |
| `no_mention` | 3 | 0 |

### 39 同品牌三款（L3_apple_three_models，16 条）

| 出口 | 改造前基线 | 阶段一严格档 |
| --- | --- | --- |
| `hit_exact` | 7 | 7 |
| `clarify` | 1 | 3 |
| `confirm_clarify` | 0 | 2 |
| `brand_giveup` | 1 | 1 |
| `no_brand_group` | 4 | 1 |
| `single_candidate` | 2 | 1 |
| `unlisted_brand` | 0 | 1 |
| `no_mention` | 1 | 0 |

### 40 同品牌十款（L5_apple_ten_models，14 条）

| 出口 | 改造前基线 | 阶段一严格档 |
| --- | --- | --- |
| `hit_exact` | 5 | 5 |
| `clarify` | 1 | 3 |
| `brand_giveup` | 2 | 2 |
| `confirm_clarify` | 0 | 2 |
| `no_brand_group` | 2 | 1 |
| `refuse` | 1 | 1 |
| `no_mention` | 1 | 0 |
| `single_candidate` | 2 | 0 |

### 41 空名单（L4_empty_library，6 条）

| 出口 | 改造前基线 | 阶段一严格档 |
| --- | --- | --- |
| `empty_vocab` | 5 | 6 |
| `no_mention` | 1 | 0 |

### 42 对照库（不启用实体识别）（L0_disabled_mirror，16 条）

| 出口 | 改造前基线 | 阶段一严格档 |
| --- | --- | --- |
| `not_required` | 16 | 16 |

### 合计

| 出口 | 改造前基线 | 阶段一严格档 |
| --- | --- | --- |
| `hit_exact` | 19 | 19 |
| `not_required` | 16 | 16 |
| `clarify` | 4 | 8 |
| `confirm_clarify` | 0 | 7 |
| `empty_vocab` | 5 | 6 |
| `no_brand_group` | 11 | 5 |
| `single_candidate` | 7 | 4 |
| `brand_giveup` | 3 | 3 |
| `brand_narrowed` | 2 | 2 |
| `refuse` | 2 | 2 |
| `single_product_narrowed` | 0 | 2 |
| `unlisted_brand` | 0 | 2 |
| `no_mention` | 7 | 0 |

### 逐条差异（出口或收窄名单变了，共 16 条）

| 库 | 行 | 问法 | 基线出口 | 严格档出口 | 基线收窄 | 严格档收窄 | 严格档反问候选 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 37 | 6 | 支持 wifi7 吗 | `no_brand_group` | `single_product_narrowed` | - | 一加 Ace 6T | - |
| 37 | 8 | 保修政策是什么 | `no_mention` | `single_product_narrowed` | - | 一加 Ace 6T | - |
| 38 | 6 | 支持蓝牙 5.3 吗 | `no_mention` | `confirm_clarify` | - | - | ThinkPad X1 Carbon Gen12/华为 MatePad 11 |
| 38 | 7 | 小米 15 多少钱 | `no_brand_group` | `unlisted_brand` | - | - | - |
| 38 | 8 | 保修政策是什么 | `no_mention` | `confirm_clarify` | - | - | ThinkPad X1 Carbon Gen12/华为 MatePad 11 |
| 38 | 12 | 这两个设备哪个轻 | `no_mention` | `confirm_clarify` | - | - | ThinkPad X1 Carbon Gen12/华为 MatePad 11 |
| 39 | 6 | 支持 pps1.2 吗 | `no_brand_group` | `confirm_clarify` | - | - | iPhone 15/iPhone 15 Pro/iPhone 18 Pro |
| 39 | 7 | 小米 15 多少钱 | `no_brand_group` | `unlisted_brand` | - | - | - |
| 39 | 8 | 保修政策是什么 | `no_mention` | `confirm_clarify` | - | - | iPhone 15/iPhone 15 Pro/iPhone 18 Pro |
| 39 | 12 | 苹果15pro 电池多大 | `no_brand_group` | `clarify` | - | - | iPhone 15/iPhone 15 Pro |
| 39 | 13 | iphone15por 保修几年 | `single_candidate` | `clarify` | iPhone 15 | - | iPhone 15/iPhone 15 Pro |
| 40 | 6 | 支持 wifi8 吗 | `no_brand_group` | `confirm_clarify` | - | - | iPhone 12/iPhone 13/iPhone 14/iPhone 15/iPhone 15 Pro/iPhone 16/iPhone 17/iPhone 18 |
| 40 | 8 | 保修政策是什么 | `no_mention` | `confirm_clarify` | - | - | iPhone 12/iPhone 13/iPhone 14/iPhone 15/iPhone 15 Pro/iPhone 16/iPhone 17/iPhone 18 |
| 40 | 9 | 一家15p 电池多大 | `single_candidate` | `clarify` | iPhone 15 Pro | - | iPhone 15 Pro/iPhone 15 |
| 40 | 12 | 18 pro 和 18 哪个电池大 | `single_candidate` | `clarify` | iPhone 18 Pro | - | iPhone 18 Pro/iPhone 18 |
| 41 | 3 | 保修政策是什么 | `no_mention` | `empty_vocab` | - | - | - |

**逐字命中被改掉的那几条（一票否决项，必须逐条读）：0 条**
