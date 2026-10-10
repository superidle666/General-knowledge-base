# 探针集建库结果

- 后端：`http://127.0.0.1:8000`　生成时间：2026-10-10 10:51:48
- 每个库跑完后请用 `--inspect` 复核词表实际形状，再跑全量。

| 库 | code | mode | library_id | 投了 | ready | needs_confirm | 其他 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| P1_探针单款（L1_single_model） | `probe-l1-single` | required | **37** | 0 | 0 | 0 | 0 |
| P2_探针跨品牌（L2_two_brands） | `probe-l2-two-brands` | required | **38** | 2 | 2 | 0 | 0 |
| P3_探针苹果三款（L3_apple_three_models） | `probe-l3-apple3` | required | **39** | 3 | 3 | 0 | 0 |
| P5_探针苹果十款（L5_apple_ten_models） | `probe-l5-apple10` | required | **40** | 10 | 10 | 0 | 0 |
| P4_探针空壳库（L4_empty_library） | `probe-l4-empty` | required | **41** | 0 | 0 | 0 | 0 |
| P0_探针对照disabled（L0_disabled_mirror） | `probe-l0-disabled` | disabled | **42** | 3 | 3 | 0 | 0 |

## 需要人工确认过实体的文档（导入期就绪度的原始记录）

这些篇的 `kb_entity.source` 会是 `manual`：模型自己没能定下主实体，是脚本按正文全名兜的。
它们正是「名单认不出」那一类问题在**导入侧**的对应物，第二、三件事排期时要拿这个数说话。

（无：所有文档的实体都被模型自动抽出了，词表就绪度满分）

## 下一步：跑探针（library_id 已经填好）

```powershell
cd e:\python_project\knowledge_base
.venv\Scripts\python.exe eval\exit_probe.py --library-id 38 --library L2_two_brands --inspect
.venv\Scripts\python.exe eval\exit_probe.py --library-id 39 --library L3_apple_three_models --inspect
.venv\Scripts\python.exe eval\exit_probe.py --library-id 40 --library L5_apple_ten_models --inspect
.venv\Scripts\python.exe eval\exit_probe.py --library-id 42 --library L0_disabled_mirror --inspect
```

L4 空壳库不投文档，单独跑一次就能看到 `empty_vocab` 那一格：
`.venv\Scripts\python.exe eval\exit_probe.py --library-id 41 --library L4_empty_library`（需要先给它准备一个只有 README 的用例文件，或临时把 L3 的 cases.md 复制过去）
