import type {
  AIConfigDiffResponse,
  AIExplainResponse,
  AssistantSuggestion,
  CameraPathResponse,
  ConfigDetail,
  ConfigListItem,
  FieldPreview,
  FieldSuggestion,
  HealthResponse,
  InputCatalog,
  RunRecord,
} from "./types";

const API_BASE_URL =
  (import.meta.env.VITE_API_BASE_URL as string | undefined)?.replace(/\/$/, "") ??
  "/api/v1";

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${API_BASE_URL}${path}`, {
    headers: {
      "Content-Type": "application/json",
      ...(init?.headers ?? {}),
    },
    ...init,
  });

  if (!response.ok) {
    const text = await response.text();
    throw new Error(text || `Request failed: ${response.status}`);
  }

  return (await response.json()) as T;
}

export const api = {
  baseUrl: API_BASE_URL,
  getHealth: () => request<HealthResponse>("/health"),
  listInputs: () => request<InputCatalog>("/inputs"),
  deleteInput: (name: string) =>
    request<{ name: string; path: string; deleted: boolean }>(`/inputs?name=${encodeURIComponent(name)}`, {
      method: "DELETE",
    }),
  captureFieldPreview: (body: Record<string, unknown>) =>
    request<FieldPreview>("/inputs/field-preview", {
      method: "POST",
      body: JSON.stringify(body),
    }),
  suggestFieldSetup: (body: Record<string, unknown>) =>
    request<FieldSuggestion>("/inputs/field-suggestion", {
      method: "POST",
      body: JSON.stringify(body),
    }),
  listConfigs: () => request<ConfigListItem[]>("/configs"),
  deleteConfig: (name: string) =>
    request<{ name: string; path: string; deleted: boolean }>(`/configs?name=${encodeURIComponent(name)}`, {
      method: "DELETE",
    }),
  getConfig: (name: string) => request<ConfigDetail>(`/configs/${encodeURIComponent(name)}`),
  deriveConfig: (body: Record<string, unknown>) =>
    request<ConfigDetail>("/configs/derive", {
      method: "POST",
      body: JSON.stringify(body),
    }),
  listRuns: () => request<RunRecord[]>("/runs"),
  getRun: (runId: string) => request<RunRecord>(`/runs/${encodeURIComponent(runId)}`),
  getCleanupReport: (runId: string) => request<Record<string, unknown>>(`/runs/${encodeURIComponent(runId)}/cleanup-report`),
  getFollowCamReport: (runId: string) =>
    request<Record<string, unknown>>(`/runs/${encodeURIComponent(runId)}/follow-cam-report`),
  getCameraPath: (runId: string, limit = 50) =>
    request<CameraPathResponse>(`/runs/${encodeURIComponent(runId)}/camera-path?limit=${limit}`),
  createRun: (body: Record<string, unknown>) =>
    request<RunRecord>("/runs", {
      method: "POST",
      body: JSON.stringify(body),
    }),
  createFollowCamRender: (runId: string, body: Record<string, unknown>) =>
    request<RunRecord>(`/runs/${encodeURIComponent(runId)}/follow-cam-render`, {
      method: "POST",
      body: JSON.stringify(body),
    }),
  aiExplain: (body: Record<string, unknown>) =>
    request<AIExplainResponse>("/ai/explain", {
      method: "POST",
      body: JSON.stringify(body),
    }),
  aiRecommend: (body: Record<string, unknown>) =>
    request<
      AssistantSuggestion & {
        expected_tradeoff?: string;
        patch?: Record<string, unknown>;
        patch_preview?: string[];
        output_name_suggestion?: string | null;
      }
    >("/ai/recommend", {
      method: "POST",
      body: JSON.stringify(body),
    }).then((response) => ({
      ...response,
      patchPreview: response.patchPreview ?? response.patch_preview ?? [],
      outputNameSuggestion: response.outputNameSuggestion ?? response.output_name_suggestion ?? null,
    })),
  aiConfigDiff: (body: Record<string, unknown>) =>
    request<
      AIConfigDiffResponse & {
        patch_preview?: string[];
      }
    >("/ai/config-diff", {
      method: "POST",
      body: JSON.stringify(body),
    }).then((response) => ({
      ...response,
      patch_preview: response.patch_preview ?? [],
    })),
  artifactUrl: (runId: string, artifactName: string) =>
    `${API_BASE_URL}/runs/${encodeURIComponent(runId)}/artifacts/${encodeURIComponent(artifactName)}`,
};
