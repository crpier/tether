import type { components } from "../generated";
import { ApiError } from "./error";
import { requireData, type RestContext } from "./transport";

export type AgentModel = components["schemas"]["AgentModelRead"];
export type Conversation = components["schemas"]["ConversationRead"];
export type Message = components["schemas"]["MessageRead"];
export type ModelList = components["schemas"]["ModelListRead"];

export interface ListMessagesOptions {
  limit?: number;
  beforeSeq?: number;
}

export interface ChatHost {
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
  synthesizeSpeech(text: string, signal: AbortSignal): Promise<Blob>;
  transcribeAudio(blob: Blob): Promise<string>;
}

export function createChatHost(context: RestContext): ChatHost {
  return {
    async listConversations() {
      const { data, response } = await context.client.GET("/api/conversations");
      return requireData(data, response);
    },
    async listMessages(conversationId, options) {
      const { data, response } = await context.client.GET(
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
      const { data, response } = await context.client.DELETE(
        "/api/conversations/{conversation_id}/messages",
        { params: { path: { conversation_id: conversationId } } },
      );
      return requireData(data, response);
    },
    async listModels() {
      const { data, response } = await context.client.GET("/api/models");
      return requireData(data, response);
    },
    async setConversationModel(conversationId, selectedModel) {
      const { data, response } = await context.client.POST(
        "/api/conversations/{conversation_id}/model",
        {
          body: { selected_model: selectedModel },
          params: { path: { conversation_id: conversationId } },
        },
      );
      return requireData(data, response);
    },
    async synthesizeSpeech(text, signal) {
      const response = await context.fetch("/api/tts/speech", {
        body: JSON.stringify({ text }),
        credentials: "include",
        headers: { "Content-Type": "application/json" },
        method: "POST",
        signal,
      });
      if (!response.ok) {
        throw new ApiError(response.status, {
          502: "Speech generation failed. Please try again.",
          503: "Speech generation is temporarily unavailable.",
        });
      }
      return response.blob();
    },
    async transcribeAudio(blob) {
      const body = new FormData();
      body.append("file", blob, "recording.webm");
      const response = await context.fetch("/api/stt/transcriptions", {
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
