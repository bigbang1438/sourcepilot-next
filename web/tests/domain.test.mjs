import { test } from "node:test";
import assert from "node:assert/strict";
import * as domain from "../src/domain.ts";
import {
  pageUrl,
  safeImageUrl,
  availableActions,
  writeHeaders,
  loadWorkspace,
} from "../src/domain.ts";

test("取消请求未释放执行槽位时不能提供继续按钮", () => {
  assert.deepEqual(availableActions("cancelled", true), {cancel: false, resume: false});
  assert.deepEqual(availableActions("cancelled", false), {cancel: false, resume: true});
});

test("视觉转录引用明确提示核对原页，不能冒充原文", () => {
  const label = domain.citationSourceLabel?.({sourceType: "model_visual"}) || "";
  assert.match(label, /图像识别/);
  assert.match(label, /核对原页/);
});
test("引用打开确切文档版本与页码，特殊文档ID不能改变路由", () => {
  assert.equal(
    pageUrl({ documentId: "paper/a?", name: "论文", version: 4, page: 12 }),
    "/api/documents/paper%2Fa%3F/pages/12?version=4",
  );
  assert.throws(() =>
    pageUrl({ documentId: "id", name: "论文", version: 0, page: -1 }),
  );
});
test("原文图片只允许同源受控文档资源，拒绝脚本和外部跟踪图片", () => {
  assert.equal(
    safeImageUrl("/api/documents/a/images/1"),
    "/api/documents/a/images/1",
  );
  assert.equal(
    safeImageUrl("/api/assets/" + "a".repeat(64) + ".png"),
    "/api/assets/" + "a".repeat(64) + ".png",
  );
  for (const unsafe of [
    "javascript:alert(1)",
    "https://tracker.test/x",
    "//tracker.test/x",
    "/private/secret",
  ])
    assert.equal(safeImageUrl(unsafe), undefined);
});
test("运行中可取消而可恢复状态提供续跑，完成任务不重复恢复", () => {
  assert.deepEqual(availableActions("running"), {
    cancel: true,
    resume: false,
  });
  assert.deepEqual(availableActions("cancelled"), {
    cancel: false,
    resume: true,
  });
  assert.deepEqual(availableActions("interrupted"), {
    cancel: false,
    resume: true,
  });
  assert.deepEqual(availableActions("budget_exceeded"), {
    cancel: false,
    resume: true,
  });
  assert.deepEqual(availableActions("completed"), {
    cancel: false,
    resume: false,
  });
});
test("所有写请求使用本机工作区请求标记", () => {
  assert.equal(writeHeaders()["X-Requested-With"], "SourcePilot");
});
test("模型探测未返回时资料与历史仍可打开，随后就绪状态会独立显示", async () => {
  let finishHealth;
  let visibleHealth = null;
  const health = new Promise((resolve) => (finishHealth = resolve));
  const results = await loadWorkspace(
    [Promise.resolve("documents"), Promise.resolve("runs")],
    health,
    (value) => (visibleHealth = value),
    () => {},
  );
  assert.deepEqual(results, [
    { status: "fulfilled", value: "documents" },
    { status: "fulfilled", value: "runs" },
  ]);
  assert.equal(visibleHealth, null);
  finishHealth({ modelReady: true });
  await health;
  await Promise.resolve();
  assert.deepEqual(visibleHealth, { modelReady: true });
});
