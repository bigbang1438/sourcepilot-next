# 独立审查后的两个 P2 修复

> 公开源码范围：本文保留原开发环境的实施与验证历史。提到的日志、截图、仓库外 QA/研究原始材料不在此公开目录；当前可用测试和质量限制见 [EVIDENCE 摘要](../EVIDENCE.md)。


依据 `../../research/business-rebuild-security-review-2026-10-17.md`，范围仅本副本 server，无模型调用，无 web 修改。

## 视觉来源兼容分支

复现路径为图片上传、检索、旧 `citations` 格式直接完成，未先读页。旧代码只看检索观察中的可选 `sourceType`，因扫描转录也存在于存储页文字而误标 `document_text`。

修复后兼容引用明确读取对应任务快照版本的 page，并保留 page 的 `model_visual`。新短句柄主路径与正常文字来源保持原协议。测试同时检查最终引用类型和报告中的视觉模型提示；没有把这个测试当成视觉识别正确率。

## 完成/取消提交次序

两个可控并发复现分别让取消在报告保存时返回、让取消先读到 running 后延迟写入。旧代码可使取消响应 cancelled 后最终变 completed，或完成后又被旧取消判断覆盖为 cancelled。

修复将取消的重查/写入和 worker 最后预算检查/提交放在同一 `runner.lock`：

- 取消先取得锁并提交：worker 随后读到 cancel，报告不保存、答案不写入。
- 完成先进入最终提交：报告与 completed 状态/答案/引用在同一 SQLite 事务内，以 running 且 cancel=0 为条件更新；并发取消等待完成后返回 completed，不能覆盖。
- 通过真实 SQLite 触发器拒绝 completed 更新，检查事务失败不会遗留已提交报告。执行失败仍明确显示 failed。

这只证明本机单进程应用的提交一致性，不是跨服务分布式事务或外部模型副作用恰好一次保证。`executionActive` 仍由 worker finally 释放，服务端 GPU 是否立即停止仍不在保证内。

## 新回归与记录

`tests/test_review_boundaries.py` 共 5 项（并发交错参数化两项）：

1. 检索后旧引用完成保留视觉来源。
2. 完成先赢，取消响应/最终状态/报告一致（取消直接返回交错）。
3. 完成先赢，取消旧读值不能覆盖（延迟读值交错）。
4. 取消先赢，无答案/无报告且执行最终释放。
5. SQLite 完成事务失败，无孤立报告。

`red-review-boundaries.log`：4 项失败、1 项原先通过；`green-review-boundaries.log`：5 项通过。全套及静态检查以 `green-review-regression.log` 和根代理独立结果为准。

尚未跑实际聊天/嵌入/视觉模型，此记录不证明官方资料任务已经通过。
