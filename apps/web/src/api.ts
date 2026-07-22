import { createTetherClient } from "./generated";
import type { components, TetherClient } from "./generated";
import { httpStatusMessage, type HttpStatusMessages } from "./lib/http-errors";

export type AgentModel = components["schemas"]["AgentModelRead"];
export type Conversation = components["schemas"]["ConversationRead"];
export type Message = components["schemas"]["MessageRead"];
export type ModelList = components["schemas"]["ModelListRead"];
export type Session = components["schemas"]["SessionResponse"];
export type Trigger = components["schemas"]["TriggerRead"];
export type CreateTrigger = components["schemas"]["CreateTriggerRequest"];
export type UpdateTrigger = components["schemas"]["UpdateTriggerRequest"];
export type PushStatus = components["schemas"]["PushStatusRead"];
export type TriggerRecurrence = components["schemas"]["TriggerRecurrence"];
export type TriggerActionKind = components["schemas"]["TriggerActionKind"];
export type DuePrompt = components["schemas"]["DuePromptRead"];
export type AnswerOutcome = components["schemas"]["AnswerOutcomeRead"];
export type EssayGradeProposal =
  components["schemas"]["EssayGradeProposalRead"];

type AnswerPromptRequest = components["schemas"]["AnswerPromptRequest"];

// The answer input for a recall prompt, derived from the generated contract
// (ADR 0008): multiple choice sends selected_index, short answer sends
// answer_text, essay sends answer_text plus the human-confirmed
// confirmed_correct (the model only proposes a grade). The per-kind fields are
// optional here; the client fills the wire's `null` defaults.
// Pagination window for `listMessages`: `limit` caps the page size (the
// default full-history fetch omits both), `beforeSeq` is the cursor for
// "older than this seq" — the oldest `seq` seen so far in the accumulated
// transcript, for walking further back in history.
export interface ListMessagesOptions {
  limit?: number;
  beforeSeq?: number;
}

export type RecallAnswerInput = Pick<AnswerPromptRequest, "response_ms"> &
  Partial<
    Pick<
      AnswerPromptRequest,
      "answer_text" | "confirmed_correct" | "selected_index"
    >
  >;
export type YouTubeSyncStatus = components["schemas"]["YouTubeSyncStatusRead"];
export type Notification = components["schemas"]["NotificationRead"];
export type BucketItem = components["schemas"]["BucketItemRead"];
export type BucketItemType = components["schemas"]["ItemType"];
export type BucketItemState = components["schemas"]["BucketItemState"];
export type AddBucketItem = components["schemas"]["AddBucketItemRequest"];
export type BucketItemAdded = components["schemas"]["AddBucketItemResponse"];
export type DedupAdvisory = components["schemas"]["DedupAdvisoryRead"];
export type BucketTriageReport = components["schemas"]["TriageReport"];
export type Todo = components["schemas"]["TodoRead"];
export type TodoStatus = components["schemas"]["TodoStatus"];
export type TodoReadiness = components["schemas"]["TodoReadinessRead"];
export type Memory = components["schemas"]["MemoryRead"];
export type MemoryState = components["schemas"]["MemoryState"];
export type Artifact = components["schemas"]["ArtifactRead"];
export type ArtifactEvent = components["schemas"]["ArtifactEventRead"];
export type Panel = components["schemas"]["PanelRead"];
export type PanelResults = components["schemas"]["PanelResultsRead"];
export type CreatePanel = components["schemas"]["CreatePanelRequest"];
export type UpdatePanel = components["schemas"]["UpdatePanelRequest"];

export interface TetherApi {
  getSession(): Promise<Session>;
  login(password: string): Promise<void>;
  logout(): Promise<void>;
  listConversations(): Promise<Conversation[]>;
  listMessages(
    conversationId: string,
    options?: ListMessagesOptions,
  ): Promise<Message[]>;
  clearConversation(conversationId: string): Promise<Conversation>;
  listModels(): Promise<ModelList>;
  setConversationModel(
    conversationId: string,
    selectedModel: string,
  ): Promise<Conversation>;
  listTriggers(): Promise<Trigger[]>;
  createTrigger(body: CreateTrigger): Promise<Trigger>;
  updateTrigger(triggerId: string, body: UpdateTrigger): Promise<Trigger>;
  deleteTrigger(triggerId: string, version: number): Promise<void>;
  getPushStatus(endpoint: string): Promise<PushStatus>;
  subscribePush(endpoint: string, p256dh: string, auth: string): Promise<void>;
  unsubscribePush(endpoint: string): Promise<PushStatus>;
  getYouTubeSyncStatus(): Promise<YouTubeSyncStatus>;
  listDueRecallPrompts(): Promise<DuePrompt[]>;
  answerRecallPrompt(
    promptId: string,
    input: RecallAnswerInput,
  ): Promise<AnswerOutcome>;
  proposeEssayGrade(
    promptId: string,
    answerText: string,
  ): Promise<EssayGradeProposal>;
  listNotifications(): Promise<Notification[]>;
  dismissNotification(notificationId: string): Promise<void>;
  clearNotifications(): Promise<void>;
  listBucketItems(state: BucketItemState): Promise<BucketItem[]>;
  searchBucketItems(q: string): Promise<BucketItem[]>;
  addBucketItem(body: AddBucketItem): Promise<BucketItemAdded>;
  completeBucketItem(
    bucketItemId: string,
    version: number,
  ): Promise<BucketItem>;
  deleteBucketItem(bucketItemId: string, version: number): Promise<BucketItem>;
  getBucketTriage(): Promise<BucketTriageReport>;
  listTodos(): Promise<TodoReadiness>;
  setTodoStatus(
    todoId: string,
    status: TodoStatus,
    version: number,
  ): Promise<Todo>;
  listMemories(state: MemoryState): Promise<Memory[]>;
  searchMemories(q: string): Promise<Memory[]>;
  captureMemory(content: string): Promise<Memory>;
  editMemory(
    memoryId: string,
    content: string,
    version: number,
  ): Promise<Memory>;
  tetherMemory(memoryId: string, version: number): Promise<Memory>;
  rejectMemory(memoryId: string, version: number): Promise<Memory>;
  // Fetches an artifact's latest version, `html` included — the viewer calls
  // this fresh on every open (no pre-fetch/cache warm, see #188), so a
  // re-open always reflects the current latest version.
  getArtifact(artifactId: string): Promise<Artifact>;
  // Relays one opaque `postMessage` payload from a sandboxed artifact's
  // viewer to the host, under the browser's own session (ADR 0011's sole
  // talk-back channel).
  postArtifactEvent(
    artifactId: string,
    payload: Record<string, unknown>,
  ): Promise<ArtifactEvent>;
  listPanels(): Promise<Panel[]>;
  createPanel(body: CreatePanel): Promise<Panel>;
  updatePanel(panelId: string, body: UpdatePanel): Promise<Panel>;
  deletePanel(panelId: string, version: number): Promise<Panel>;
  // A panel execution is a Search, recomputed on every call (ADR 0006) — the
  // caller never caches results beyond the query layer's own invalidation.
  getPanelResults(panelId: string, limit?: number): Promise<PanelResults>;
  // Transcribe-only voice input (issue #19): uploads a recorded clip and
  // returns the transcript text only — no Memory is created and no chat turn
  // is injected server-side; the caller (the chat composer) decides what to
  // do with the transcript. Not routed through the generated client: the host
  // route takes a raw multipart body with no typed OpenAPI request schema
  // (see `tether/stt_routes.py`), so this issues a plain `fetch` instead.
  transcribeAudio(blob: Blob): Promise<string>;
}

// Carries the HTTP status so callers can react to specific failures (e.g. a 409
// version conflict on a fired trigger) rather than only surfacing the raw text.
// The message is the friendly, human-readable text for that status.
export class ApiError extends Error {
  readonly status: number;

  constructor(status: number, messages?: HttpStatusMessages) {
    super(httpStatusMessage(status, messages));
    this.name = "ApiError";
    this.status = status;
  }
}

function requireData<T>(
  data: T | undefined,
  response: Response,
  messages?: HttpStatusMessages,
): T {
  if (!response.ok) {
    throw new ApiError(response.status, messages);
  }
  if (data === undefined) {
    throw new Error("Request returned no data");
  }
  return data;
}

function requireOk(response: Response, messages?: HttpStatusMessages): void {
  if (!response.ok) {
    throw new ApiError(response.status, messages);
  }
}

export function createRestApi(
  client: TetherClient = createTetherClient(),
): TetherApi {
  return {
    async getSession() {
      const { data, response } = await client.GET("/api/auth/session");
      return requireData(data, response);
    },
    async login(password) {
      const { response } = await client.POST("/api/auth/login", {
        body: { password },
      });
      requireOk(response, { 401: "Incorrect password." });
    },
    async logout() {
      const { response } = await client.POST("/api/auth/logout");
      requireOk(response);
    },
    async listConversations() {
      const { data, response } = await client.GET("/api/conversations");
      return requireData(data, response);
    },
    async listMessages(conversationId, options) {
      const { data, response } = await client.GET(
        "/api/conversations/{conversation_id}/messages",
        {
          params: {
            path: { conversation_id: conversationId },
            query: {
              limit: options?.limit,
              before_seq: options?.beforeSeq,
            },
          },
        },
      );
      return requireData(data, response);
    },
    async clearConversation(conversationId) {
      const { data, response } = await client.DELETE(
        "/api/conversations/{conversation_id}/messages",
        { params: { path: { conversation_id: conversationId } } },
      );
      return requireData(data, response);
    },
    async listModels() {
      const { data, response } = await client.GET("/api/models");
      return requireData(data, response);
    },
    async setConversationModel(conversationId, selectedModel) {
      const { data, response } = await client.POST(
        "/api/conversations/{conversation_id}/model",
        {
          body: { selected_model: selectedModel },
          params: { path: { conversation_id: conversationId } },
        },
      );
      return requireData(data, response);
    },
    async listTriggers() {
      const { data, response } = await client.GET("/api/triggers");
      return requireData(data, response);
    },
    async createTrigger(body) {
      const { data, response } = await client.POST("/api/triggers", { body });
      return requireData(data, response);
    },
    async updateTrigger(triggerId, body) {
      const { data, response } = await client.PUT(
        "/api/triggers/{trigger_id}",
        { body, params: { path: { trigger_id: triggerId } } },
      );
      return requireData(data, response);
    },
    async deleteTrigger(triggerId, version) {
      const { response } = await client.DELETE("/api/triggers/{trigger_id}", {
        params: { path: { trigger_id: triggerId }, query: { version } },
      });
      requireOk(response);
    },
    async getPushStatus(endpoint) {
      const { data, response } = await client.GET("/api/push/status", {
        params: { query: { endpoint } },
      });
      return requireData(data, response);
    },
    async subscribePush(endpoint, p256dh, auth) {
      const { response } = await client.POST("/api/push/subscriptions", {
        body: { endpoint, p256dh, auth },
      });
      requireOk(response);
    },
    async unsubscribePush(endpoint) {
      const { data, response } = await client.DELETE(
        "/api/push/subscriptions",
        { body: { endpoint } },
      );
      return requireData(data, response);
    },
    async getYouTubeSyncStatus() {
      const { data, response } = await client.GET("/api/youtube/status");
      return requireData(data, response);
    },
    async listDueRecallPrompts() {
      const { data, response } = await client.GET("/api/recall/prompts");
      return requireData(data, response);
    },
    async answerRecallPrompt(promptId, input) {
      const { data, response } = await client.POST(
        "/api/recall/prompts/{prompt_id}/answer",
        {
          body: {
            answer_text: input.answer_text ?? null,
            confirmed_correct: input.confirmed_correct ?? null,
            response_ms: input.response_ms,
            selected_index: input.selected_index ?? null,
          },
          params: { path: { prompt_id: promptId } },
        },
      );
      return requireData(data, response);
    },
    async proposeEssayGrade(promptId, answerText) {
      const { data, response } = await client.POST(
        "/api/recall/prompts/{prompt_id}/grade-proposal",
        {
          body: { answer_text: answerText },
          params: { path: { prompt_id: promptId } },
        },
      );
      return requireData(data, response);
    },
    async listNotifications() {
      const { data, response } = await client.GET("/api/notifications");
      return requireData(data, response);
    },
    async dismissNotification(notificationId) {
      const { response } = await client.DELETE(
        "/api/notifications/{notification_id}",
        { params: { path: { notification_id: notificationId } } },
      );
      requireOk(response);
    },
    async clearNotifications() {
      const { response } = await client.DELETE("/api/notifications");
      requireOk(response);
    },
    async listBucketItems(state) {
      const { data, response } = await client.GET("/api/bucket-items", {
        params: { query: { state } },
      });
      return requireData(data, response);
    },
    async searchBucketItems(q) {
      const { data, response } = await client.GET("/api/bucket-items/search", {
        params: { query: { q } },
      });
      return requireData(data, response);
    },
    async addBucketItem(body) {
      const { data, response } = await client.POST("/api/bucket-items", {
        body,
      });
      return requireData(data, response);
    },
    async completeBucketItem(bucketItemId, version) {
      const { data, response } = await client.POST(
        "/api/bucket-items/{bucket_item_id}/complete",
        {
          body: { version },
          params: { path: { bucket_item_id: bucketItemId } },
        },
      );
      return requireData(data, response);
    },
    async deleteBucketItem(bucketItemId, version) {
      const { data, response } = await client.DELETE(
        "/api/bucket-items/{bucket_item_id}",
        {
          params: {
            path: { bucket_item_id: bucketItemId },
            query: { version },
          },
        },
      );
      return requireData(data, response);
    },
    async getBucketTriage() {
      const { data, response } = await client.GET("/api/bucket-items/triage");
      return requireData(data, response);
    },
    async listTodos() {
      const { data, response } = await client.GET("/api/todos");
      return requireData(data, response);
    },
    async setTodoStatus(todoId, status, version) {
      const { data, response } = await client.POST(
        "/api/todos/{todo_id}/status",
        {
          body: { status, version },
          params: { path: { todo_id: todoId } },
        },
      );
      return requireData(data, response);
    },
    async listMemories(state) {
      const { data, response } = await client.GET("/api/memories", {
        params: { query: { state } },
      });
      return requireData(data, response);
    },
    async searchMemories(q) {
      const { data, response } = await client.GET("/api/memories/search", {
        params: { query: { q } },
      });
      return requireData(data, response);
    },
    async captureMemory(content) {
      const { data, response } = await client.POST("/api/memories", {
        body: { content },
      });
      return requireData(data, response);
    },
    async editMemory(memoryId, content, version) {
      const { data, response } = await client.PATCH(
        "/api/memories/{memory_id}",
        {
          body: { content, version },
          params: { path: { memory_id: memoryId } },
        },
      );
      return requireData(data, response);
    },
    async tetherMemory(memoryId, version) {
      const { data, response } = await client.POST(
        "/api/memories/{memory_id}/tether",
        {
          body: { version },
          params: { path: { memory_id: memoryId } },
        },
      );
      return requireData(data, response);
    },
    async rejectMemory(memoryId, version) {
      const { data, response } = await client.DELETE(
        "/api/memories/{memory_id}",
        {
          params: { path: { memory_id: memoryId }, query: { version } },
        },
      );
      return requireData(data, response);
    },
    async getArtifact(artifactId) {
      const { data, response } = await client.GET(
        "/api/artifacts/{artifact_id}",
        { params: { path: { artifact_id: artifactId } } },
      );
      return requireData(data, response);
    },
    async postArtifactEvent(artifactId, payload) {
      const { data, response } = await client.POST(
        "/api/artifacts/{artifact_id}/events",
        {
          body: { payload },
          params: { path: { artifact_id: artifactId } },
        },
      );
      return requireData(data, response);
    },
    async listPanels() {
      const { data, response } = await client.GET("/api/panels");
      return requireData(data, response);
    },
    async createPanel(body) {
      const { data, response } = await client.POST("/api/panels", { body });
      return requireData(data, response);
    },
    async updatePanel(panelId, body) {
      const { data, response } = await client.PUT("/api/panels/{panel_id}", {
        body,
        params: { path: { panel_id: panelId } },
      });
      return requireData(data, response);
    },
    async deletePanel(panelId, version) {
      const { data, response } = await client.DELETE("/api/panels/{panel_id}", {
        params: { path: { panel_id: panelId }, query: { version } },
      });
      return requireData(data, response);
    },
    async getPanelResults(panelId, limit) {
      const { data, response } = await client.GET(
        "/api/panels/{panel_id}/results",
        {
          params: {
            path: { panel_id: panelId },
            query: limit === undefined ? {} : { limit },
          },
        },
      );
      return requireData(data, response);
    },
    async transcribeAudio(blob) {
      const body = new FormData();
      body.append("file", blob, "recording.webm");
      const response = await fetch("/api/stt/transcriptions", {
        body,
        credentials: "include",
        method: "POST",
      });
      let data: { transcript?: string } | undefined;
      try {
        data = (await response.json()) as { transcript?: string };
      } catch {
        data = undefined;
      }
      if (!response.ok || data?.transcript === undefined) {
        throw new ApiError(response.status, {
          413: "That recording is too long to transcribe.",
          422: "No speech was detected in that recording.",
          502: "Transcription failed. Please try again.",
          503: "Transcription is temporarily unavailable. Please try again shortly.",
        });
      }
      return data.transcript;
    },
  };
}
