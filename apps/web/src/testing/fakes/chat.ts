import { ApiError } from "../../host/error";
import type {
  Attachment,
  ChatHost,
  Conversation,
  ConversationTurn,
  CreateConversation,
  ListMessagesOptions,
  Message,
  UpdateConversation,
} from "../../host/chat";
import { conversation, models } from "../fixtures";

export class FakeChatHost implements ChatHost {
  messageCalls = 0;
  selectedModel: string | undefined;
  storedConversation: Conversation = { ...conversation };
  storedConversations: Conversation[] = [this.storedConversation];
  storedMessages: Message[];
  storedTurns: ConversationTurn[] = [];
  archiveConversationCalls: string[] = [];
  archiveConversationRejections: Error[] = [];
  createConversationCalls: CreateConversation[] = [];
  fetchConversationRejections: Error[] = [];
  listMessagesCalls: (ListMessagesOptions | undefined)[] = [];
  markConversationReadCalls: {
    conversationId: string;
    lastReadSeq: number;
  }[] = [];
  markConversationReadRejections: Error[] = [];
  restoreConversationCalls: string[] = [];
  restoreConversationRejections: Error[] = [];
  updateConversationCalls: {
    body: UpdateConversation;
    conversationId: string;
  }[] = [];
  synthesizeSpeechCalls: string[] = [];
  synthesizeSpeechRejections: ApiError[] = [];
  transcribeAudioCalls: Blob[] = [];
  transcribeAudioRejections: ApiError[] = [];
  undoGmailArchiveCalls: string[] = [];
  uploadAttachmentCalls: File[] = [];
  nextTranscript = "";

  constructor(messages: Message[] = []) {
    this.storedMessages = messages;
  }

  archiveConversation(conversationId: string) {
    this.archiveConversationCalls.push(conversationId);
    const forced = this.archiveConversationRejections.shift();
    if (forced !== undefined) {
      return Promise.reject(forced);
    }
    return this.mutateConversation(conversationId, {
      archived_at: "2026-01-03T00:00:00Z",
      status: "archived",
    });
  }

  createConversation(body: CreateConversation) {
    this.createConversationCalls.push(body);
    const sequence = (900 + this.createConversationCalls.length)
      .toString()
      .padStart(12, "0");
    const created: Conversation = {
      ...conversation,
      display_name: body.display_name?.trim() ?? null,
      id: `018f0000-0000-7000-8000-${sequence}`,
      kind: "scoped",
      scope_brief: body.scope_brief?.trim() ?? null,
      title: body.display_name?.trim() ?? null,
    };
    this.storedConversations = [...this.storedConversations, created];
    return Promise.resolve(created);
  }

  fetchConversation(conversationId: string) {
    const forced = this.fetchConversationRejections.shift();
    if (forced !== undefined) {
      return Promise.reject(forced);
    }
    const found = this.allConversations().find(
      (candidate) => candidate.id === conversationId,
    );
    return found === undefined
      ? Promise.reject(new ApiError(404))
      : Promise.resolve(found);
  }

  fetchTurn(conversationId: string, turnId: string) {
    const found = this.storedTurns.find(
      (turn) => turn.conversation_id === conversationId && turn.id === turnId,
    );
    return found === undefined
      ? Promise.reject(new ApiError(404))
      : Promise.resolve(found);
  }

  listConversations(options?: { includeArchived?: boolean }) {
    return Promise.resolve(
      options?.includeArchived === true
        ? this.allConversations()
        : this.allConversations().filter(
            (candidate) => candidate.status === "active",
          ),
    );
  }

  listMessages(conversationId: string, options?: ListMessagesOptions) {
    this.messageCalls += 1;
    this.listMessagesCalls.push(options);
    const matchingConversation = this.storedMessages.filter(
      (candidate) => candidate.conversation_id === conversationId,
    );
    const matchingTurn =
      options?.turnId === undefined
        ? matchingConversation
        : matchingConversation.filter(
            (candidate) => candidate.turn_id === options.turnId,
          );
    const windowed =
      options?.beforeSeq === undefined
        ? matchingTurn
        : matchingTurn.filter(
            (candidate) => candidate.seq < (options.beforeSeq ?? Infinity),
          );
    const page =
      options?.limit === undefined
        ? windowed
        : windowed.slice(Math.max(0, windowed.length - options.limit));
    return Promise.resolve(page);
  }

  listNonterminalTurns(conversationId: string) {
    return Promise.resolve(
      this.storedTurns.filter(
        (turn) =>
          turn.conversation_id === conversationId &&
          (turn.status === "pending" || turn.status === "running"),
      ),
    );
  }

  listModels() {
    return Promise.resolve(models);
  }

  markConversationRead(conversationId: string, lastReadSeq: number) {
    this.markConversationReadCalls.push({ conversationId, lastReadSeq });
    const forced = this.markConversationReadRejections.shift();
    if (forced !== undefined) {
      return Promise.reject(forced);
    }
    return this.mutateConversation(conversationId, {
      has_unread: false,
      last_read_seq: lastReadSeq,
    });
  }

  restoreConversation(conversationId: string) {
    this.restoreConversationCalls.push(conversationId);
    const forced = this.restoreConversationRejections.shift();
    if (forced !== undefined) {
      return Promise.reject(forced);
    }
    return this.mutateConversation(conversationId, {
      archived_at: null,
      status: "active",
    });
  }

  setConversationModel(conversationId: string, selectedModel: string) {
    this.selectedModel = selectedModel;
    return this.mutateConversation(conversationId, {
      selected_model: selectedModel,
    });
  }

  updateConversation(conversationId: string, body: UpdateConversation) {
    this.updateConversationCalls.push({ body, conversationId });
    return this.mutateConversation(conversationId, {
      ...(body.display_name === undefined
        ? {}
        : { display_name: body.display_name, title: body.display_name }),
      ...(body.scope_brief === undefined
        ? {}
        : { scope_brief: body.scope_brief }),
    });
  }

  undoGmailArchive(messageId: string) {
    this.undoGmailArchiveCalls.push(messageId);
    return Promise.resolve({
      detail: null,
      message_id: messageId,
      outcome: "done" as const,
    });
  }

  synthesizeSpeech(text: string, signal: AbortSignal): Promise<Blob> {
    void signal;
    this.synthesizeSpeechCalls.push(text);
    const forced = this.synthesizeSpeechRejections.shift();
    if (forced !== undefined) {
      return Promise.reject(forced);
    }
    return Promise.resolve(
      Object.assign(new Blob([text], { type: "audio/mpeg" }), {
        speechText: text,
      }),
    );
  }

  transcribeAudio(blob: Blob): Promise<string> {
    this.transcribeAudioCalls.push(blob);
    const forced = this.transcribeAudioRejections.shift();
    if (forced !== undefined) {
      return Promise.reject(forced);
    }
    return Promise.resolve(this.nextTranscript);
  }

  uploadAttachment(conversationId: string, file: File): Promise<Attachment> {
    void conversationId;
    this.uploadAttachmentCalls.push(file);
    return Promise.resolve({
      filename: file.name,
      id: "018f0000-0000-7000-8000-000000000099",
      kind: file.type.startsWith("image/") ? "image" : "document",
      mime_type: file.type,
      size_bytes: file.size,
    });
  }

  private allConversations(): Conversation[] {
    return this.storedConversations.map((candidate) =>
      candidate.id === this.storedConversation.id
        ? this.storedConversation
        : candidate,
    );
  }

  private mutateConversation(
    conversationId: string,
    patch: Partial<Conversation>,
  ): Promise<Conversation> {
    const found = this.allConversations().find(
      (candidate) => candidate.id === conversationId,
    );
    if (found === undefined) {
      return Promise.reject(new ApiError(404));
    }
    const updated = { ...found, ...patch };
    this.storedConversations = this.storedConversations.map((candidate) =>
      candidate.id === conversationId ? updated : candidate,
    );
    if (this.storedConversation.id === conversationId) {
      this.storedConversation = updated;
    }
    return Promise.resolve(updated);
  }
}
