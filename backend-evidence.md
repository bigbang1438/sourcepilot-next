# 修订副本的证据与未验证边界

> 公开源码范围：本文保留原开发环境的实施与验证历史。提到的日志、截图、仓库外 QA/研究原始材料不在此公开目录；当前可用测试和质量限制见 [EVIDENCE 摘要](EVIDENCE.md)。


记录日期：2026-10-17。此文档对应 `sourcepilot-next`，原 `sourcepilot` 与旧失败数据不变。根代理已完成真实模型两批复验；本页区分程序交付完成与独立内容评分，不把完成或引文存在当作事实正确。

## 验证起因

原真实资料批次：`../qa/business-2026-10-17/ai-business-results.json`、`post-run-reconciliation.json`。选定六题，三个创建、三个 429；已创建任务终态为两个取消、一个失败，没有答案可作事实评分。解释见 `../research/ai-real-task-scoring-2026-10-17.md`。引用格式阻断、简单事实额外看图、超时后继续发任务及取消/槽位混淆，构成本次修复依据。

## 新增行为回归

文件：`server/tests/test_business_repairs.py`。

| 测试 | 捕获的行为破坏 | 范围 |
| --- | --- | --- |
| `test_short_handles_fill_exact_snapshot_citation_without_model_copying_uuid` | 已观察短证据不能回填真实 ID、版本、页码和原句 | 真实 Store/Agent/API，受控模型决策 |
| `test_unknown_evidence_handle_cannot_become_a_valid_citation` | 未观察的 e999 被接受完成 | 必须拒绝，不交付伪造引用 |
| `test_repeated_read_does_not_repeat_page_body_and_keeps_observed_handles` | 同页再次发正文或失去原证据 | 连续重复读路径 |
| `test_searching_an_already_read_page_keeps_read_cache_and_original_evidence` | 再次检索覆盖已读状态，重复发全文 | 读页→同页检索→读页完整路径 |
| `test_scan_transcription_is_labelled_model_visual_not_document_original` | 模型转录错误标记为原文 | 不证明识别正确 |
| `test_budget_is_checked_after_model_returns_not_after_next_graph_node` | 超出预算仍执行工具或没有累计用量 | 可控时钟跨过预算 |
| `test_cancelled_response_exposes_occupied_slot_until_worker_really_exits` | 取消被误当退出、槽位不透明、运行中重复恢复 | 事件阻塞模型，仍保持两任务上限 |
| `test_repeated_stream_data_cannot_extend_total_model_request_deadline` | 整个模型请求无限等待 | 受控 AsyncClient 延迟；不是原生网络分块或服务端 GPU 取消实测 |

原 26 项回归覆盖版本、失败上传、去重、BM25 回退、NumPy/RRF、受限工具与伪造引用、图像/扫描页、步骤/Token/剩余时限、取消恢复、真实子进程退出检查点恢复、报告幂等、巨大栅格预检和本机接口安全。新副本三处模型 HTTP 替身适配异步接口，断言意图保留；原目录测试不改。

## RED / GREEN，不删失败

- `server/red-business.log`：最初七项全部失败。
- `server/green-business-first.log`：首次修复后仍有关闭竞态、用量可见性和请求计时失败。
- `server/green-business-second.log`：七项通过。
- `server/green-regression-first.log`：带覆盖率全套 32 通过、1 失败，覆盖约 88%；0.05 秒请求截止测试测得 0.375 秒，超过 0.3 秒断言，未放宽。覆盖率/冷启动影响短时限测试，该日志不是全套通过证据。
- 根代理随后独立无覆盖率全套 33 项通过，ruff 检查和格式检查通过；类型检查所需的新副本测试对象访问改动由根代理完成，原断言不变。
- `server/red-read-cache.log`：新补缓存复现失败，重复读未命中缓存而最终任务失败。
- `server/green-regression-cache.log`：缓存最小修复后全套 **34 项通过，22.24 秒**。同次 ruff 检查通过，15 个文件格式检查通过；随后 `ty check --python` 指向原已安装依赖解释器，全套通过。
- `server/red-review-boundaries.log`：独立审查后新增五项中四失败、一项原先通过；`server/green-review-boundaries-final.log`：五项全部通过。覆盖旧引用直接引用视觉页、两个完成/取消交错、取消先赢无报告，以及真实 SQLite 提交失败不留孤立报告。
- `server/green-review-regression.log`：最终全套 **39 项通过，18.38 秒**；ruff 检查、16 个文件格式检查及 ty 通过。事务/来源修复细节见 `server/REVIEW_REPAIRS_2026-10-17.md`。

复用已安装解释器执行，已确认 `sourcepilot.agent.__file__` 来自当前 `sourcepilot-next/server`。本工作代理没有启动模型或持久服务，没有复制旧虚拟环境/数据，没有安装新框架。

复验命令（在 `server`）：

```powershell
python -m pytest
python -m ruff check sourcepilot tests
python -m ruff format --check sourcepilot tests
python -m ty check --python <已安装依赖的Python解释器>
```

## 实现入口

- `server/sourcepilot/agent.py`：受限图回路、短页面/证据、旧协议校验、连续原文片段、活动壁钟和 finally 释放。
- `server/sourcepilot/models.py`：总请求截止、原生取消事件、紧凑输入、截断拒绝；连接失败不伪造结果。
- `server/sourcepilot/api.py`：原子创建/两任务保护、`executionActive`、取消意图和恢复 409。
- `server/sourcepilot/store.py`：`source_type`、版本原子替换、任务快照和报告去重。

## 不能据此宣称的结论

1. 新增回归不证明实际模型正确选证据或停止冗余工具；已执行的两批模型复验仍有事实、范围和任务路径问题，见后文独立人工评分。
2. 短 handle 证明引用传输更可靠、来源不可任意编造，不是语义正确率。片段可能不相关或漏跨段条件。
3. `document_text` 不保证复杂 PDF 顺序/表格提取正确；`model_visual` 不能等同逐字 PDF 原文。
4. 客户端模型请求关闭、应用 worker 退出，不等于 GPU 计算同一刻结束。
5. 未提交外部推理可能重放，强制退出前未持久化时间/用量可能遗失，不能承诺完整计费或全部副作用恰好一次。
6. 尚无真实客户、人工计时基线、线上使用数据、大规模性能或多租户隔离，不能写业务效率提升百分比或企业级落地。

## 真实模型复验已执行，内容评分单列

根代理使用实际本机聊天、嵌入和视觉模型配置，另存 `../qa/business-rebuild-2026-10-17/ai-original-results.json` 与 `ai-heldout-results.json`。原冻结六题均 completed、报告 HTTP 200、`executionActive=false`；新资料四题同样完成。十份报告的交付状态不能当成十题答对。两个结果汇总中早先保存的 `humanScoring: pending` 保留原样，最终人工判断另存评分文件，不回填原运行数据。

决策模型设置为 `think=false`、`temperature=0`、`num_ctx=8192`、`num_predict=700`，模型保活 `10m`。本次应用修复之外还调整了驱动的等待/执行释放处理和保活设置；因此原失败批次与本批不是严格因果对照，不能将完成或用时变化独立归因给某一项代码修复。

原六题独立评分见[原题人工评分：公开摘要](EVIDENCE.md)和 [JSON：公开摘要](EVIDENCE.md)：

- RP04、RP09、RP10 综合 correct：PPS 结论、版本/日期边界、拒绝兼容和唯一根因保证分别得到支持。
- RP03 partial：USB 总电流比较主结论正确，但 5V/5.1V 动态电压范围与协商解释超出所引证据；流程还出现看图截断与无效工具。
- RP08 partial：评分者渲染原页后确认数值与限制正确，但模型仅使用文字抽取，未调用看图，不能证明空间位置核对或多模态能力通过。
- RP01 incorrect：600mA/1.6A 数值对，却把所有 USB 口合计说成每口，与关键引文相反。

总计 **3 correct、2 partial、1 incorrect**。其中事实完整性 4/1/1、引文语义支持 3/2/1、任务路径 4/1/1，分别按 correct/partial/incorrect 排列，不能只取“引文存在”维度。

新资料四题的独立评分记录为[新资料人工评分：公开摘要](EVIDENCE.md)和 [JSON：公开摘要](EVIDENCE.md)。该独立评分逐题读取原始结果，检查完整六页提取层并渲染 PDF 第 1–3 页：

- H01 correct：架构选项、主频、SRAM、板载闪存容量正确；未单独问 QSPI 接口，省略不算核心遗漏。
- H02 correct：仅 W 的板载无线条件、2.4GHz 802.11n、Bluetooth 5.2 正确。
- H03 correct：一个 USB 1.1、host/device 正确，并合理拒绝从简报保证任意 SSD 启动；未进行硬件实测。
- H04 partial：两个日期及非保修判断正确，但指定产品承诺被泛化为“整个产品线”。

总计 3 correct、1 partial、0 incorrect，七条引用存在于 PDF 提取文字；两者不能混成引用存在率等于答案正确率。这四题没有 `inspect_figure`，不证明视觉质量提升。逐题脚本耗时约 14.422–16.375 秒，没有人工完成时间对照。仅一份额外官方资料的四个构造问题，不能称统计意义的泛化测试或真实客户收益。原失败仍保留。

## 评分后的草稿提示

根代理将界面答案标题改为“结论草稿”，并标“尚未人工复核”；服务端新生成的 Markdown 报告增加相同边界提示，说明出处可定位不代表结论正确。既有原始验收报告未重写，因此不能要求旧报告带后来添加的提示，也不能把 UI 文案当作内容质量修复。

根代理在该标签变更后运行八项相关回归，保存于 `server/green-final-report-labels.log`，八项通过；此前全套 39 项的记录仍为 `server/green-review-regression.log`，两次检查范围分开。UI 冒烟记录在 `../qa/business-rebuild-2026-10-17/ai-ui-final.log`。这些程序检查未重跑十题模型答案，更没有补出真实用户效率。

冻结原题与独立新资料复验另存 `../qa/business-rebuild-2026-10-17/`，不能回填原失败。应分别报告创建失败、无答案、检索页命中、答案正确性、具体引用支持、无依据断言、耗时、步骤、Token 与 worker 释放状态。


> 公开源码说明：本文部分链接原本指向开发工作区外的 QA/研究材料，本仓库没有附这些运行数据、截图或原始资料；已改为仓库内 EVIDENCE 摘要，不代表原始记录被一并发布。
