export type Citation = {
  documentId: string;
  name: string;
  version: number;
  page: number;
  quote?: string;
  sourceType?: string;
};
export function pageUrl(citation: Citation): string {
  if (
    !citation.documentId ||
    !Number.isInteger(citation.page) ||
    citation.page < 1 ||
    !Number.isInteger(citation.version) ||
    citation.version < 1
  )
    throw new Error("引用缺少有效页码或版本");
  return `/api/documents/${encodeURIComponent(citation.documentId)}/pages/${citation.page}?version=${citation.version}`;
}
export function safeImageUrl(value: string | undefined): string | undefined {
  if (
    !value ||
    (!value.startsWith("/api/documents/") &&
      !/^\/api\/assets\/[a-f0-9]{64}\.(png|jpg|jpeg)$/i.test(value)) ||
    value.includes("..") ||
    value.includes("\\") ||
    /[\r\n]/.test(value)
  )
    return undefined;
  return value;
}
export function availableActions(status: string, executionActive = false): {
  cancel: boolean;
  resume: boolean;
} {
  return {
    cancel: ["pending", "queued", "running"].includes(status),
    resume: !executionActive && [
      "cancelled",
      "interrupted",
      "failed",
      "paused",
      "error",
      "budget_exceeded",
    ].includes(status),
  };
}
export function citationSourceLabel(citation: {sourceType?: string}): string {
  return citation.sourceType === "model_visual"
    ? "图像识别结果，需核对原页"
    : citation.sourceType === "document_text"
      ? "资料原文片段"
      : "资料引用，请核对原页";
}
export function writeHeaders(): Record<string, string> {
  return { "X-Requested-With": "SourcePilot" };
}
export async function loadWorkspace<T>(
  primary: Promise<unknown>[],
  health: Promise<T>,
  onHealth: (value: T) => void,
  onHealthError: (error: unknown) => void,
): Promise<PromiseSettledResult<unknown>[]> {
  void health.then(onHealth).catch(onHealthError);
  return Promise.allSettled(primary);
}
