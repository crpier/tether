import { ApiError } from "../../host/error";
import type {
  ChatHost,
  Conversation,
  ListMessagesOptions,
  Message,
} from "../../host/chat";
import { conversation, models } from "../fixtures";

export class FakeChatHost implements ChatHost {
  messageCalls = 0;
  clearConversationCalls = 0;
  selectedModel: string | undefined;
  storedConversation: Conversation = { ...conversation };
  storedMessages: Message[];
  listMessagesCalls: (ListMessagesOptions | undefined)[] = [];
  synthesizeSpeechCalls: string[] = [];
  synthesizeSpeechRejections: ApiError[] = [];
  transcribeAudioCalls: Blob[] = [];
  transcribeAudioRejections: ApiError[] = [];
  nextTranscript = "";

  constructor(messages: Message[] = []) {
    this.storedMessages = messages;
  }

  listConversations() {
    return Promise.resolve([this.storedConversation]);
  }

  listMessages(_conversationId: string, options?: ListMessagesOptions) {
    this.messageCalls += 1;
    this.listMessagesCalls.push(options);
    const windowed =
      options?.beforeSeq === undefined
        ? this.storedMessages
        : this.storedMessages.filter(
            (candidate) => candidate.seq < (options.beforeSeq ?? Infinity),
          );
    const page =
      options?.limit === undefined
        ? windowed
        : windowed.slice(Math.max(0, windowed.length - options.limit));
    return Promise.resolve(page);
  }

  clearConversation() {
    this.clearConversationCalls += 1;
    this.storedMessages = [];
    this.storedConversation = {
      ...this.storedConversation,
      pi_session_id: `018f0000-0000-7000-8000-00000000c${this.clearConversationCalls
        .toString()
        .padStart(3, "0")}`,
    };
    return Promise.resolve(this.storedConversation);
  }

  listModels() {
    return Promise.resolve(models);
  }

  setConversationModel(_conversationId: string, selectedModel: string) {
    this.selectedModel = selectedModel;
    this.storedConversation = {
      ...this.storedConversation,
      selected_model: selectedModel,
    };
    return Promise.resolve(this.storedConversation);
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
}
