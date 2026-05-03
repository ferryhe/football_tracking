import type {
  AIExplainResponse,
  AISuggestion,
  AssetGroup,
  ConfigDetail,
  ConfigListItem,
  CreateRunRequest,
  DeriveConfigRequest,
  FieldPreviewResponse,
  FieldSuggestionResponse,
  FollowCamRenderRequest,
  HealthResponse,
  InputCatalogResponse,
  RunRecord,
  UpdateConfigRequest,
} from "./types";

const BASE = "/api";

function encodePathSegmented(path: string): string {
  return path.split("/").map((segment) => encodeURIComponent(segment)).join("/");
}

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
  getConfig: (name: string) => request<ConfigDetail>(`/configs/${encodePathSegmented(name)}`),
  updateConfig: (name: string, body: UpdateConfigRequest) =>
    request<ConfigDetail>(`/configs/${encodePathSegmented(name)}`, { method: "PUT", body: JSON.stringify(body) }),
  deriveConfig: (body: DeriveConfigRequest) =>
    request<ConfigDetail>("/configs/derive", { method: "POST", body: JSON.stringify({ patch: {}, ...body }) }),
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
  cancelRun: (runId: string) =>
    request<RunRecord>(`/runs/${encodeURIComponent(runId)}/cancel`, { method: "POST" }),
  deleteRunOutput: (runId: string) =>
    request<{ name: string; path: string; deleted: boolean }>(`/runs?run_id=${encodeURIComponent(runId)}`, { method: "DELETE" }),
  deleteConfig: (name: string) =>
    request<{ name: string; path: string; deleted: boolean }>(`/configs?name=${encodeURIComponent(name)}`, { method: "DELETE" }),
  deleteInputVideo: (name: string) =>
    request<{ name: string; path: string; deleted: boolean }>(`/inputs?name=${encodeURIComponent(name)}`, { method: "DELETE" }),
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
