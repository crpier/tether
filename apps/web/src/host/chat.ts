import type { components } from "../generated";
import { ApiError } from "./error";
import { requireData, type RestContext } from "./transport";

export type AgentModel = components["schemas"]["AgentModelRead"];
export type Attachment = components["schemas"]["AttachmentRead"];
export type Conversation = components["schemas"]["ConversationRead"];
export type ConversationTurn = components["schemas"]["ConversationTurnRead"];
export type CreateConversation =
  components["schemas"]["CreateConversationRequest"];
export type UpdateConversation =
  components["schemas"]["UpdateConversationRequest"];
export type Message = components["schemas"]["MessageRead"];
export type ModelList = components["schemas"]["ModelListRead"];
export type GmailUndo = components["schemas"]["GmailUndoRead"];

export function conversationLabel(
  conversation: Conversation,
  conversations: readonly Conversation[] = [],
): string {
  if (conversation.kind === "main") {
    return "Main Chat";
  }
  const name = conversation.display_name ?? "Untitled chat";
  const duplicateCount = conversations.filter(
    (candidate) =>
      candidate.kind === "scoped" &&
      candidate.display_name === conversation.display_name,
  ).length;
  if (duplicateCount < 2) {
    return name;
  }
  const scope = conversation.scope_brief?.trim();
  const scopedLabel =
    scope === undefined || scope.length === 0 ? name : `${name} · ${scope}`;
  const exactDuplicateCount = conversations.filter(
    (candidate) =>
      candidate.kind === "scoped" &&
      candidate.display_name === conversation.display_name &&
      candidate.scope_brief?.trim() === scope,
  ).length;
  return exactDuplicateCount < 2
    ? scopedLabel
    : `${scopedLabel} · ${conversation.id.slice(-6)}`;
}

export interface ListMessagesOptions {
  limit?: number;
  beforeSeq?: number;
  turnId?: string;
}

export type ConversationArchiveBlocker =
  "active_prompt_trigger" | "nonterminal_turn";

export class ConversationArchiveBlockedError extends Error {
  constructor(public readonly blocker: ConversationArchiveBlocker) {
    super("Conversation archive blocked");
  }
}

export interface ChatHost {
  archiveConversation(conversationId: string): Promise<Conversation>;
  createConversation(body: CreateConversation): Promise<Conversation>;
  fetchConversation(conversationId: string): Promise<Conversation>;
  listConversations(options?: {
    includeArchived?: boolean;
  }): Promise<Conversation[]>;
  fetchTurn(conversationId: string, turnId: string): Promise<ConversationTurn>;
  listMessages(
    conversationId: string,
    options?: ListMessagesOptions,
  ): Promise<Message[]>;
  listNonterminalTurns(conversationId: string): Promise<ConversationTurn[]>;
  listModels(): Promise<ModelList>;
  markConversationRead(
    conversationId: string,
    lastReadSeq: number,
  ): Promise<Conversation>;
  restoreConversation(conversationId: string): Promise<Conversation>;
  setConversationModel(
    conversationId: string,
    selectedModel: string,
  ): Promise<Conversation>;
  updateConversation(
    conversationId: string,
    body: UpdateConversation,
  ): Promise<Conversation>;
  undoGmailArchive(messageId: string): Promise<GmailUndo>;
  synthesizeSpeech(text: string, signal: AbortSignal): Promise<Blob>;
  transcribeAudio(blob: Blob): Promise<string>;
  uploadAttachment(conversationId: string, file: File): Promise<Attachment>;
}

export function createChatHost(context: RestContext): ChatHost {
  return {
    async archiveConversation(conversationId) {
      const { data, error, response } = await context.client.POST(
        "/api/conversations/{conversation_id}/archive",
        { params: { path: { conversation_id: conversationId } } },
      );
      if (!response.ok && response.status === 409) {
        const blocker = (error as { blocker?: unknown } | undefined)?.blocker;
        if (
          blocker === "active_prompt_trigger" ||
          blocker === "nonterminal_turn"
        ) {
          throw new ConversationArchiveBlockedError(blocker);
        }
      }
      return requireData(data, response);
    },
    async createConversation(body) {
      const { data, response } = await context.client.POST(
        "/api/conversations",
        { body },
      );
      return requireData(data, response);
    },
    async fetchConversation(conversationId) {
      const { data, response } = await context.client.GET(
        "/api/conversations/{conversation_id}",
        { params: { path: { conversation_id: conversationId } } },
      );
      return requireData(data, response);
    },
    async listConversations(options) {
      const { data, response } = await context.client.GET(
        "/api/conversations",
        {
          params: { query: { include_archived: options?.includeArchived } },
        },
      );
      return requireData(data, response);
    },
    async fetchTurn(conversationId, turnId) {
      const { data, response } = await context.client.GET(
        "/api/conversations/{conversation_id}/turns/{turn_id}",
        {
          params: {
            path: { conversation_id: conversationId, turn_id: turnId },
          },
        },
      );
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
              turn_id: options?.turnId,
            },
          },
        },
      );
      return requireData(data, response);
    },
    async listNonterminalTurns(conversationId) {
      const { data, response } = await context.client.GET(
        "/api/conversations/{conversation_id}/turns",
        { params: { path: { conversation_id: conversationId } } },
      );
      return requireData(data, response);
    },
    async listModels() {
      const { data, response } = await context.client.GET("/api/models");
      return requireData(data, response);
    },
    async markConversationRead(conversationId, lastReadSeq) {
      const { data, response } = await context.client.POST(
        "/api/conversations/{conversation_id}/read",
        {
          body: { last_read_seq: lastReadSeq },
          params: { path: { conversation_id: conversationId } },
        },
      );
      return requireData(data, response);
    },
    async restoreConversation(conversationId) {
      const { data, response } = await context.client.POST(
        "/api/conversations/{conversation_id}/restore",
        { params: { path: { conversation_id: conversationId } } },
      );
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
    async updateConversation(conversationId, body) {
      const { data, response } = await context.client.PATCH(
        "/api/conversations/{conversation_id}",
        {
          body,
          params: { path: { conversation_id: conversationId } },
        },
      );
      return requireData(data, response);
    },
    async undoGmailArchive(messageId) {
      const { data, response } = await context.client.POST(
        "/api/gmail/actions/undo",
        { body: { action: "archive", message_id: messageId } },
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
    async uploadAttachment(conversationId, file) {
      const body = new FormData();
      body.append("file", file, file.name);
      const response = await context.fetch(
        `/api/conversations/${conversationId}/attachments`,
        {
          body,
          credentials: "include",
          method: "POST",
        },
      );
      if (!response.ok) {
        throw new ApiError(response.status, {
          413: "That file is larger than 10 MB.",
          422: "Tether supports images, PDFs, and UTF-8 text files.",
        });
      }
      return (await response.json()) as Attachment;
    },
  };
}
