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

// The answer input for a recall prompt, shaped by its kind: multiple choice
// sends selectedIndex, short answer sends answerText, essay sends answerText
// plus the human-confirmed confirmedCorrect (the model only proposes a grade).
export interface RecallAnswerInput {
  selectedIndex?: number;
  answerText?: string;
  confirmedCorrect?: boolean;
  responseMs: number;
}
export type YouTubeSyncStatus = components["schemas"]["YouTubeSyncStatusRead"];
export type Notification = components["schemas"]["NotificationRead"];

export interface TetherApi {
  getSession(): Promise<Session>;
  login(password: string): Promise<void>;
  logout(): Promise<void>;
  listConversations(): Promise<Conversation[]>;
  listMessages(conversationId: string): Promise<Message[]>;
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
    async listMessages(conversationId) {
      const { data, response } = await client.GET(
        "/api/conversations/{conversation_id}/messages",
        { params: { path: { conversation_id: conversationId } } },
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
            answer_text: input.answerText ?? null,
            confirmed_correct: input.confirmedCorrect ?? null,
            response_ms: input.responseMs,
            selected_index: input.selectedIndex ?? null,
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
  };
}
