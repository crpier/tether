import { createTetherClient } from "./generated";
import type { components, TetherClient } from "./generated";

export type AgentModel = components["schemas"]["AgentModelRead"];
export type Conversation = components["schemas"]["ConversationRead"];
export type Message = components["schemas"]["MessageRead"];
export type ModelList = components["schemas"]["ModelListRead"];
export type Session = components["schemas"]["SessionResponse"];

export interface TetherApi {
  getSession(): Promise<Session>;
  login(password: string): Promise<void>;
  logout(): Promise<void>;
  listConversations(): Promise<Conversation[]>;
  listMessages(conversationId: string): Promise<Message[]>;
  listModels(): Promise<ModelList>;
  setConversationModel(
    conversationId: string,
    selectedModel: string,
  ): Promise<Conversation>;
}

function requireData<T>(data: T | undefined, response: Response): T {
  if (!response.ok) {
    throw new Error(`Request failed: ${String(response.status)}`);
  }
  if (data === undefined) {
    throw new Error("Request returned no data");
  }
  return data;
}

function requireOk(response: Response): void {
  if (!response.ok) {
    throw new Error(`Request failed: ${String(response.status)}`);
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
      requireOk(response);
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
  };
}
