<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from "vue";
import {
  pageUrl,
  safeImageUrl,
  availableActions,
  citationSourceLabel,
  writeHeaders,
  loadWorkspace,
  type Citation,
} from "./domain";
type Document = {
  id: string;
  name: string;
  version: number;
  status: string;
  kind: string;
  pages: number;
  error?: string;
};
type Step = { index: number; tool: string; status: string; summary: string };
type Run = {
  id: string;
  status: string;
  question: string;
  steps: Step[];
  answer?: string;
  citations?: Citation[];
  error?: string;
  reportUrl?: string;
  executionActive?: boolean;
  cancellationRequested?: boolean;
};
type Hit = Citation & { text: string; score: number; kind?: string };
const documents = ref<Document[]>([]),
  runs = ref<Run[]>([]),
  selectedIds = ref<string[]>([]),
  selectedRun = ref<Run | null>(null),
  question = ref(""),
  searchQuery = ref(""),
  searchMode = ref("hybrid"),
  hits = ref<Hit[]>([]),
  actualMode = ref(""),
  reranked = ref(false);
const error = ref(""),
  notice = ref(""),
  initializing = ref(true),
  uploading = ref(false),
  creating = ref(false),
  searching = ref(false),
  reading = ref(false),
  reader = ref<any>(null),
  readerQuote = ref(""),
  activeView = ref("research"),
  sidebar = ref(false),
  loadingRun = ref(false),
  actionBusy = ref(false),
  health = ref<any>(null),
  fileInput = ref<HTMLInputElement>();
let poll: number | undefined,
  pollBusy = false,
  readerEpoch = 0,
  runEpoch = 0,
  alive = true;
const statusNames: Record<string, string> = {
  queued: "等待研读",
  pending: "等待研读",
  running: "正在研读",
  completed: "已完成",
  success: "已完成",
  done: "已完成",
  failed: "未完成",
  cancelled: "已取消",
  interrupted: "已中断",
  budget_exceeded: "达到本轮限额",
  ready: "可研读",
  processing: "处理中",
  indexed: "可研读",
  error: "处理失败",
  paused: "已暂停",
};
const toolNames: Record<string, string> = {
  search: "查找资料",
  read_page: "阅读原文",
  inspect_figure: "查看图片",
  finish: "整理结论",
};
const action = computed(() =>
    availableActions(selectedRun.value?.status || "", selectedRun.value?.executionActive),
  ),
  selectionLabel = computed(() =>
    selectedIds.value.length
      ? `已选 ${selectedIds.value.length} 份资料`
      : "全部可用资料",
  );
const selectedSet = computed(() => new Set(selectedIds.value)),
  readyDocs = computed(() =>
    documents.value.filter(
      (d) => !["failed", "error", "processing"].includes(d.status),
    ),
  );
const activeCount = computed(
  () =>
    runs.value.filter((r) => r.executionActive || ["running", "queued"].includes(r.status)).length,
);
const displayStatus = (status: string) => statusNames[status] || status;
async function api(path: string, options: RequestInit = {}) {
  const headers = new Headers(options.headers);
  if (options.method && options.method !== "GET")
    for (const [k, v] of Object.entries(writeHeaders())) headers.set(k, v);
  if (options.body && !(options.body instanceof FormData))
    headers.set("Content-Type", "application/json");
  const response = await fetch(path, { ...options, headers });
  const data = await response
    .json()
    .catch(() => ({ message: "无法读取服务响应，请重试" }));
  if (!response.ok)
    throw new Error(data.message || data.detail || "操作失败，请重试");
  return data;
}
async function refreshDocuments() {
  documents.value = await api("/api/documents");
  selectedIds.value = selectedIds.value.filter((id) =>
    documents.value.some((d) => d.id === id),
  );
}
async function refreshRuns() {
  runs.value = await api("/api/runs");
}
async function initialize() {
  initializing.value = true;
  error.value = "";
  try {
    const result = await loadWorkspace(
      [refreshDocuments(), refreshRuns()],
      api("/api/health"),
      (data) => {
        if (alive) health.value = data;
      },
      (e) => {
        if (alive)
          notice.value = `暂时无法确认模型状态：${(e as Error).message}`;
      },
    );
    for (const r of result)
      if (r.status === "rejected") error.value = (r.reason as Error).message;
    const id = new URLSearchParams(location.search).get("run");
    if (id) await openRun(id);
  } finally {
    initializing.value = false;
  }
}
async function upload(files: FileList | null) {
  if (!files?.length) return;
  uploading.value = true;
  error.value = "";
  let success = 0;
  for (const file of Array.from(files)) {
    try {
      const form = new FormData();
      form.append("file", file);
      await api("/api/documents", { method: "POST", body: form });
      success++;
    } catch (e) {
      error.value = `${file.name}：${(e as Error).message}`;
    }
  }
  try {
    await refreshDocuments();
    if (success)
      notice.value = `已接收 ${success} 份资料。同名资料将保留版本记录。`;
  } catch (e) {
    error.value = (e as Error).message;
  } finally {
    uploading.value = false;
    if (fileInput.value) fileInput.value.value = "";
  }
}
function toggleDocument(id: string) {
  selectedIds.value = selectedSet.value.has(id)
    ? selectedIds.value.filter((x) => x !== id)
    : [...selectedIds.value, id];
}
function newTask() {
  selectedRun.value = null;
  question.value = "";
  activeView.value = "research";
  sidebar.value = false;
  history.replaceState(null, "", location.pathname);
}
async function createRun() {
  if (!question.value.trim() || !readyDocs.value.length) return;
  creating.value = true;
  error.value = "";
  try {
    const run = await api("/api/runs", {
      method: "POST",
      body: JSON.stringify({
        question: question.value.trim(),
        ...(selectedIds.value.length ? { documentIds: selectedIds.value } : {}),
      }),
    });
    await refreshRuns();
    await openRun(run.id);
  } catch (e) {
    error.value = (e as Error).message;
  } finally {
    creating.value = false;
  }
}
async function openRun(id: string) {
  const epoch = ++runEpoch;
  activeView.value = "research";
  sidebar.value = false;
  loadingRun.value = true;
  error.value = "";
  try {
    const run = await api(`/api/runs/${encodeURIComponent(id)}`);
    if (epoch !== runEpoch) return;
    selectedRun.value = run;
    history.replaceState(null, "", `?run=${encodeURIComponent(id)}`);
  } catch (e) {
    error.value = (e as Error).message;
  } finally {
    if (epoch === runEpoch) loadingRun.value = false;
  }
}
async function runAction(kind: "cancel" | "resume") {
  if (!selectedRun.value || actionBusy.value) return;
  actionBusy.value = true;
  error.value = "";
  try {
    const run = await api(
      `/api/runs/${encodeURIComponent(selectedRun.value.id)}/${kind}`,
      { method: "POST" },
    );
    selectedRun.value = run;
    await refreshRuns();
    notice.value =
      kind === "cancel" ? (run.executionActive ? "已请求停止，正在结束当前处理。已完成步骤会保留。" : "任务已取消，已完成的步骤会保留。") : "已继续研读。";
  } catch (e) {
    error.value = (e as Error).message;
  } finally {
    actionBusy.value = false;
  }
}
async function pollRun() {
  if (pollBusy || !alive) return;
  pollBusy = true;
  try {
    const active = selectedRun.value;
    if (active && (active.executionActive || availableActions(active.status, active.executionActive).cancel)) {
      const run = await api(`/api/runs/${encodeURIComponent(active.id)}`);
      if (selectedRun.value?.id === active.id) selectedRun.value = run;
      if (!run.executionActive && !availableActions(run.status, run.executionActive).cancel) await refreshRuns();
    } else if (activeCount.value) await refreshRuns();
    if (documents.value.some((d) => d.status === "processing"))
      await refreshDocuments();
  } catch (e) {
    if (alive) notice.value = `暂时无法刷新进度：${(e as Error).message}`;
  } finally {
    pollBusy = false;
  }
}
async function search() {
  if (!searchQuery.value.trim()) return;
  searching.value = true;
  error.value = "";
  hits.value = [];
  actualMode.value = "";
  try {
    const data = await api("/api/search", {
      method: "POST",
      body: JSON.stringify({
        query: searchQuery.value.trim(),
        mode: searchMode.value,
        limit: 10,
        ...(selectedIds.value.length ? { documentIds: selectedIds.value } : {}),
      }),
    });
    hits.value = data.hits;
    actualMode.value = data.mode;
    reranked.value = data.reranked;
  } catch (e) {
    error.value = (e as Error).message;
  } finally {
    searching.value = false;
  }
}
async function readCitation(citation: Citation) {
  const epoch = ++readerEpoch;
  reading.value = true;
  reader.value = null;
  readerQuote.value = citation.quote || "";
  try {
    const page = await api(pageUrl(citation));
    if (epoch === readerEpoch) reader.value = page;
  } catch (e) {
    error.value = (e as Error).message;
  } finally {
    if (epoch === readerEpoch) reading.value = false;
  }
}
function closeReader() {
  readerEpoch++;
  reader.value = null;
  reading.value = false;
  readerQuote.value = "";
}
function readDocument(doc: Document) {
  void readCitation({
    documentId: doc.id,
    name: doc.name,
    version: doc.version,
    page: 1,
  });
}
async function changePage(page: number) {
  if (!reader.value || page < 1) return;
  await readCitation({
    documentId: reader.value.documentId,
    name: reader.value.name,
    version: reader.value.version,
    page,
  });
}
const readerDocument = computed(() =>
  documents.value.find(
    (d) =>
      d.id === reader.value?.documentId && d.version === reader.value?.version,
  ),
);
function keydown(event: KeyboardEvent) {
  if (event.key === "Escape") {
    closeReader();
    sidebar.value = false;
  }
}
onMounted(() => {
  void initialize();
  poll = window.setInterval(pollRun, 1700);
  window.addEventListener("keydown", keydown);
});
onBeforeUnmount(() => {
  alive = false;
  window.clearInterval(poll);
  window.removeEventListener("keydown", keydown);
});
</script>

<template>
  <main class="research-app" :class="{ 'reader-open': reader || reading }">
    <button
      class="mobile-menu"
      aria-label="打开资料与历史"
      @click="sidebar = !sidebar"
    >
      ☰
    </button>
    <div v-if="sidebar" class="mobile-shade" @click="sidebar = false"></div>
    <aside class="library" :class="{ open: sidebar }">
      <a class="brand" href="/" aria-label="研读舱首页"
        ><span class="brand-glyph">◈</span
        ><span><b>研读舱</b><small>带着出处，读懂资料</small></span></a
      >
      <button class="new-task" @click="newTask">
        <span>＋</span>新建研读任务
      </button>
      <nav class="main-nav" aria-label="工作区">
        <button
          :class="{ active: activeView === 'research' }"
          @click="
            activeView = 'research';
            sidebar = false;
          "
        >
          <span>◫</span>研读工作台</button
        ><button
          :class="{ active: activeView === 'search' }"
          @click="
            activeView = 'search';
            sidebar = false;
          "
        >
          <span>⌕</span>检索资料
        </button>
      </nav>
      <div class="library-label">
        <h2>
          资料库 <small>{{ documents.length }}</small>
        </h2>
        <button
          title="刷新资料列表"
          aria-label="刷新资料列表"
          @click="refreshDocuments().catch((e) => (error = e.message))"
        >
          ↻
        </button>
      </div>
      <div v-if="initializing" class="library-empty">正在读取资料…</div>
      <div v-else-if="!documents.length" class="library-empty">
        还没有资料。<br />上传第一份文档，开始研读。
      </div>
      <div v-else class="document-list">
        <article
          v-for="doc in documents"
          :key="doc.id"
          class="document-row"
          :class="{ selected: selectedSet.has(doc.id) }"
        >
          <label class="document-check"
            ><input
              type="checkbox"
              :checked="selectedSet.has(doc.id)"
              :disabled="['failed', 'error', 'processing'].includes(doc.status)"
              :aria-label="`选择资料 ${doc.name}`"
              @change="toggleDocument(doc.id)" /><span></span></label
          ><button
            class="document-open"
            :title="doc.error || `打开 ${doc.name}`"
            @click="readDocument(doc)"
          >
            <b>{{ doc.name }}</b
            ><small
              >v{{ doc.version }} · {{ doc.pages }} 页
              <em :class="{ bad: ['failed', 'error'].includes(doc.status) }">{{
                displayStatus(doc.status)
              }}</em></small
            ></button
          ><span class="file-kind">{{ doc.kind?.toUpperCase() }}</span>
          <p v-if="doc.error" class="doc-error">{{ doc.error }}</p>
        </article>
      </div>
      <div
        class="upload-area"
        @dragover.prevent
        @drop.prevent="upload($event.dataTransfer?.files || null)"
      >
        <input
          ref="fileInput"
          type="file"
          multiple
          accept=".pdf,.md,.txt,.png,.jpg,.jpeg"
          class="visually-hidden"
          aria-label="上传资料文件"
          @change="upload(($event.target as HTMLInputElement).files)"
        /><button :disabled="uploading" @click="fileInput?.click()">
          <span>↥</span>{{ uploading ? "正在处理资料…" : "上传资料" }}</button
        ><small>PDF、Markdown、文字或图片<br />也可将文件拖到这里</small>
      </div>
      <div class="library-label history-label">
        <h2>研读记录</h2>
        <button
          aria-label="刷新研读记录"
          title="刷新记录"
          @click="refreshRuns().catch((e) => (error = e.message))"
        >
          ↻
        </button>
      </div>
      <p v-if="!runs.length" class="library-empty history-empty">
        完成的讨论会留在这里。
      </p>
      <div class="run-list">
        <button
          v-for="run in runs"
          :key="run.id"
          :class="{
            current: selectedRun?.id === run.id && activeView === 'research',
          }"
          @click="openRun(run.id)"
        >
          <span class="run-dot" :class="run.status"></span
          ><span
            ><b>{{ run.question }}</b
            ><small>{{ displayStatus(run.status) }}</small></span
          >
        </button>
      </div>
      <footer class="library-footer">
        <span class="workspace-dot"></span>本机个人工作区<span
          v-if="health"
          :title="health.modelReady ? '研读模型连接正常' : '研读模型暂不可用'"
          :class="{ unavailable: !health.modelReady }"
          >{{ health.modelReady ? "模型就绪" : "模型未就绪" }}</span
        >
      </footer>
    </aside>
    <section class="main-column">
      <header class="page-header">
        <div>
          <span class="breadcrumb"
            >我的工作区 <i>/</i>
            {{ activeView === "search" ? "检索资料" : "研读工作台" }}</span
          >
          <h1>
            {{
              activeView === "search"
                ? "从资料里找到线索"
                : selectedRun
                  ? "这一轮研读"
                  : "你想读懂什么？"
            }}
          </h1>
        </div>
        <span class="selection-pill">▤ {{ selectionLabel }}</span>
      </header>
      <div v-if="error" class="banner error" role="alert">
        <span>{{ error }}</span
        ><button aria-label="关闭错误提示" @click="error = ''">×</button>
      </div>
      <div v-if="notice" class="banner notice" role="status">
        <span>{{ notice }}</span
        ><button aria-label="关闭提示" @click="notice = ''">×</button>
      </div>
      <div v-if="initializing" class="loading-main">
        <span class="loading-ring"></span>正在打开工作区…
      </div>
      <div v-else-if="activeView === 'search'" class="main-scroll search-page">
        <p class="intro">输入关键词或一个完整问题，直接查看命中的原文片段。</p>
        <form class="search-form" @submit.prevent="search">
          <div class="search-input">
            <span>⌕</span
            ><input
              v-model="searchQuery"
              aria-label="检索问题"
              required
              placeholder="在资料中查找…"
            /><button
              class="primary"
              :disabled="searching || !readyDocs.length"
            >
              {{ searching ? "检索中…" : "检索" }}
            </button>
          </div>
          <label class="search-mode"
            >检索方式<select v-model="searchMode">
              <option value="hybrid">混合检索</option>
              <option value="vector">语义检索</option>
              <option value="bm25">词语检索</option>
            </select></label
          >
        </form>
        <div v-if="actualMode" class="result-summary">
          <b>{{
            hits.length ? `找到 ${hits.length} 条线索` : "没有找到相关片段"
          }}</b
          ><span
            >本次使用{{
              actualMode === "bm25"
                ? "词语检索"
                : actualMode === "vector"
                  ? "语义检索"
                  : "混合检索"
            }}{{ reranked ? "，已按相关性重排" : "" }}</span
          >
        </div>
        <p v-if="actualMode && !hits.length" class="empty-message">
          试试更具体的词语，或从左侧调整所选资料。
        </p>
        <div v-if="searching" class="loading-main">
          <span class="loading-ring"></span>正在查找相关片段…
        </div>
        <article
          v-for="(hit, index) in hits"
          :key="`${hit.documentId}:${hit.version}:${hit.page}:${index}`"
          class="search-hit"
        >
          <div>
            <span class="source-tag">{{ hit.kind || "原文" }}</span
            ><b>{{ hit.name }}</b
            ><small>v{{ hit.version }} · 第 {{ hit.page }} 页</small>
          </div>
          <p>{{ hit.text }}</p>
          <button class="read-link" @click="readCitation(hit)">
            打开这一页 <span>↗</span>
          </button>
        </article>
        <div v-if="!actualMode && !searching" class="search-invitation">
          <span>⌕</span>
          <h2>先找线索，再读原文。</h2>
          <p>检索不会创建研读任务。<br />每条结果都可打开对应版本的原文。</p>
        </div>
      </div>
      <div v-else-if="loadingRun" class="loading-main">
        <span class="loading-ring"></span>正在读取研读记录…
      </div>
      <div v-else-if="!selectedRun" class="main-scroll welcome">
        <div class="welcome-lead">
          <span class="reading-symbol">◈</span>
          <h2>把问题交给资料，<br />让结论有据可查。</h2>
          <p>
            选几份资料，提出你的问题。研读过程、原文出处和结论，会一起保存在这次记录里。
          </p>
        </div>
        <form class="question-composer" @submit.prevent="createRun">
          <label for="question">研读问题</label
          ><textarea
            id="question"
            v-model="question"
            placeholder="例如：这几份资料有哪些共同结论？它们的依据分别是什么？"
            rows="4"
            required
            maxlength="6000"
          ></textarea>
          <div class="composer-bottom">
            <span>▤ {{ selectionLabel }}</span
            ><button
              class="primary"
              :disabled="creating || !readyDocs.length || !question.trim()"
            >
              {{ creating ? "正在创建…" : "开始研读" }} <span>↗</span>
            </button>
          </div>
        </form>
        <div v-if="!readyDocs.length" class="start-hint">
          <span>↥</span>
          <p>
            <b>先放入一份资料</b><br />在左侧上传
            PDF、Markdown、文字或图片，即可开始。
          </p>
          <button @click="fileInput?.click()">上传资料</button>
        </div>
        <div v-else class="start-hint">
          <span>☑</span>
          <p>
            <b>研读范围由你决定</b
            ><br />勾选左侧资料可缩小范围；未勾选时使用全部可用资料。
          </p>
        </div>
        <p v-if="health && !health.modelReady" class="model-hint">
          研读模型暂不可用。可以先上传资料与检索原文，连接模型后再开始研读。
        </p>
      </div>
      <div v-else class="main-scroll run-page">
        <header class="run-heading">
          <span class="status-tag" :class="selectedRun.status"
            ><i></i>{{ selectedRun.cancellationRequested && selectedRun.executionActive ? '正在停止' : displayStatus(selectedRun.status) }}</span
          >
          <h2>{{ selectedRun.question }}</h2>
          <div class="run-actions">
            <button
              v-if="action.cancel"
              :disabled="actionBusy"
              @click="runAction('cancel')"
            >
              {{ actionBusy ? "正在处理…" : "取消任务" }}</button
            ><button
              v-if="action.resume"
              class="primary"
              :disabled="actionBusy"
              @click="runAction('resume')"
            >
              {{ actionBusy ? "正在继续…" : "继续研读" }}</button
            ><a
              v-if="selectedRun.status === 'completed'"
              class="download-button"
              :href="`/api/runs/${encodeURIComponent(selectedRun.id)}/report`"
              download
              >↓ 下载报告</a
            ><button class="read-link" @click="newTask">新建任务</button>
          </div>
        </header>
        <section class="progress-section">
          <div class="section-heading">
            <h3>研读过程</h3>
            <span>{{ selectedRun.steps?.length || 0 }} 个步骤</span>
          </div>
          <p v-if="!selectedRun.steps?.length" class="empty-message">
            {{
              action.cancel
                ? "任务已接收，正在准备阅读资料。"
                : "还没有已记录的研读步骤。"
            }}
          </p>
          <ol class="steps">
            <li
              v-for="(step, index) in selectedRun.steps"
              :key="`${step.index}:${index}`"
              :class="step.status"
            >
              <span class="step-marker">{{
                ["completed", "success", "done"].includes(step.status)
                  ? "✓"
                  : index + 1
              }}</span>
              <div>
                <header>
                  <b>{{ toolNames[step.tool] || step.tool }}</b
                  ><small>{{ displayStatus(step.status) }}</small>
                </header>
                <p>{{ step.summary }}</p>
              </div>
            </li>
          </ol>
          <div v-if="action.cancel" class="working-line">
            <span class="loading-ring small"></span
            ><span>研读正在进行，进度会自动更新。</span>
          </div>
        </section>
        <p v-if="selectedRun.error" role="alert" class="run-error">
          {{ selectedRun.error }}
        </p>
        <section v-if="selectedRun.answer" class="answer-section">
          <div class="section-heading">
            <h3>结论草稿</h3>
            <span>尚未人工复核</span>
          </div>
          <p class="no-evidence">出处可定位不代表结论正确；请逐条核对原文中的条件、范围和单位。</p>
          <div class="answer-text">{{ selectedRun.answer }}</div>
        </section>
        <section v-if="selectedRun.citations?.length" class="citations-section">
          <div class="section-heading">
            <h3>关联资料片段</h3>
            <span>点击打开对应页</span>
          </div>
          <button
            v-for="(citation, index) in selectedRun.citations"
            :key="`${citation.documentId}:${citation.page}:${index}`"
            class="citation"
            @click="readCitation(citation)"
          >
            <span class="citation-number">{{ index + 1 }}</span
            ><span
              ><b>{{ citation.name }}</b
              ><small>v{{ citation.version }} · 第 {{ citation.page }} 页 · {{ citationSourceLabel(citation) }}</small
              ><q v-if="citation.quote">{{ citation.quote }}</q></span
            ><span class="citation-arrow">↗</span>
          </button>
        </section>
        <p
          v-if="
            selectedRun.status === 'completed' && !selectedRun.citations?.length
          "
          class="no-evidence"
        >
          本次结论未附原文引用。请结合资料核实。
        </p>
      </div>
      <footer class="main-footer">
        资料只在本机工作区处理。结论请结合原文核实。
      </footer>
    </section>
    <aside
      v-if="reader || reading"
      class="reader"
      role="region"
      aria-label="原文阅读"
    >
      <header>
        <span>原文阅读</span
        ><button aria-label="关闭原文阅读" @click="closeReader">×</button>
      </header>
      <div v-if="reading" class="loading-main">
        <span class="loading-ring"></span>正在打开原文…
      </div>
      <template v-else-if="reader"
        ><div class="reader-title">
          <span class="source-tag">文档 v{{ reader.version }}</span>
          <h2>{{ reader.name }}</h2>
          <div class="page-navigation">
            <button
              :disabled="reader.page <= 1"
              aria-label="上一页"
              @click="changePage(reader.page - 1)"
            >
              ‹</button
            ><span
              >第 {{ reader.page }} 页<span v-if="readerDocument">
                / {{ readerDocument.pages }}</span
              ></span
            ><button
              :disabled="
                readerDocument ? reader.page >= readerDocument.pages : false
              "
              aria-label="下一页"
              @click="changePage(reader.page + 1)"
            >
              ›
            </button>
          </div>
        </div>
        <div class="reader-body">
          <blockquote v-if="readerQuote">
            <small>本次引用</small>{{ readerQuote }}
          </blockquote>
          <img
            v-if="safeImageUrl(reader.imageUrl)"
            :src="safeImageUrl(reader.imageUrl)"
            alt="文档这一页的原图"
          />
          <div class="original-text">
            {{ reader.text || "这一页没有可显示的文字。" }}
          </div>
        </div></template
      >
    </aside>
  </main>
</template>
