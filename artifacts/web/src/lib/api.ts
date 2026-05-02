import type {
  AIExplainResponse,
  AISuggestion,
  AssetGroup,
  ConfigListItem,
  CreateRunRequest,
  FieldPreviewResponse,
  FieldSuggestionResponse,
  FollowCamRenderRequest,
  HealthResponse,
  InputCatalogResponse,
  RunRecord,
} from "./types";

const BASE = "/api";

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${BASE}${path}`, {
    headers: { "Content-Type": "application/json", ...init?.headers },
    ...init,
  });
  if (!res.ok) {
    const text = await res.text().catch(() => res.statusText);
    throw new Error(`${res.status} ${text}`);
  }
  return res.json() as Promise<T>;
}

export const api = {
  health: () => request<HealthResponse>("/healthz"),
  listConfigs: () => request<ConfigListItem[]>("/configs"),
  listInputs: () => request<InputCatalogResponse>("/inputs"),
  listRuns: () => request<RunRecord[]>("/runs"),
  getRun: (id: string) => request<RunRecord>(`/runs/${id}`),
  listAssetGroups: () => request<AssetGroup[]>("/runs/asset-groups"),
  createRun: (body: CreateRunRequest) =>
    request<RunRecord>("/runs", { method: "POST", body: JSON.stringify(body) }),
  createFollowCamRender: (runId: string, body: FollowCamRenderRequest) =>
    request<RunRecord>(`/runs/${runId}/follow-cam-render`, {
      method: "POST",
      body: JSON.stringify(body),
    }),
  deleteRunOutput: (runId: string) =>
    request(`/runs?run_id=${encodeURIComponent(runId)}`, { method: "DELETE" }),
  deleteConfig: (name: string) =>
    request(`/configs?name=${encodeURIComponent(name)}`, { method: "DELETE" }),
  deleteInputVideo: (name: string) =>
    request(`/inputs?name=${encodeURIComponent(name)}`, { method: "DELETE" }),
  captureFieldPreview: (input_video: string, sample_index?: number) =>
    request<FieldPreviewResponse>("/inputs/field-preview", {
      method: "POST",
      body: JSON.stringify({ input_video, sample_index }),
    }),
  suggestFieldSetup: (body: { input_video: string; config_name?: string; frame_index?: number }) =>
    request<FieldSuggestionResponse>("/inputs/field-suggestion", {
      method: "POST",
      body: JSON.stringify(body),
    }),
  aiExplain: (body: { run_id?: string; config_name?: string; language?: string }) =>
    request<AIExplainResponse>("/ai/explain", {
      method: "POST",
      body: JSON.stringify(body),
    }),
  aiRecommend: (body: { run_id: string; objective?: string; language?: string }) =>
    request<AISuggestion>("/ai/recommend", {
      method: "POST",
      body: JSON.stringify(body),
    }),
};
