import type {
  BucketItem,
  DuePrompt,
  Grant,
  GrantSuggestion,
  Memory,
  Message,
  MemoryWorkspaceDiagnostic,
  Panel,
  PanelResults,
  Proposal,
  Todo,
  TranscriptDecision,
  Trigger,
  WebHost,
} from "../host";
import { FakeArtifactsHost } from "./fakes/artifacts";
import { FakeAuthHost } from "./fakes/auth";
import { FakeBucketHost } from "./fakes/bucket";
import { FakeChatHost } from "./fakes/chat";
import { FakeMemoriesHost } from "./fakes/memories";
import { FakeNotificationsHost } from "./fakes/notifications";
import { FakePanelsHost } from "./fakes/panels";
import { FakeProposalsHost } from "./fakes/proposals";
import { FakeProviderAuthHost } from "./fakes/provider-auth";
import { FakePushHost } from "./fakes/push";
import { FakeRecallHost } from "./fakes/recall";
import { FakeTodosHost } from "./fakes/todos";
import { FakeTriggersHost } from "./fakes/triggers";
import { FakeYouTubeHost } from "./fakes/youtube";

export class FakeHost implements WebHost {
  readonly artifacts = new FakeArtifactsHost();
  readonly auth: FakeAuthHost;
  readonly bucket: FakeBucketHost;
  readonly chat: FakeChatHost;
  readonly memories: FakeMemoriesHost;
  readonly notifications = new FakeNotificationsHost();
  readonly panels: FakePanelsHost;
  readonly proposals: FakeProposalsHost;
  readonly providerAuth = new FakeProviderAuthHost();
  readonly push = new FakePushHost();
  readonly recall: FakeRecallHost;
  readonly todos: FakeTodosHost;
  readonly triggers: FakeTriggersHost;
  readonly youtube: FakeYouTubeHost;

  constructor(options: {
    authenticated: boolean;
    bucketItems?: BucketItem[];
    duePrompts?: DuePrompt[];
    grants?: Grant[];
    grantSuggestions?: GrantSuggestion[];
    memories?: Memory[];
    memoryWorkspaceDiagnostics?: MemoryWorkspaceDiagnostic[];
    messages?: Message[];
    panelResults?: Record<string, PanelResults>;
    panels?: Panel[];
    proposals?: Proposal[];
    todos?: Todo[];
    transcriptDecisions?: TranscriptDecision[];
    triggers?: Trigger[];
  }) {
    this.auth = new FakeAuthHost(options.authenticated);
    this.bucket = new FakeBucketHost(options.bucketItems);
    this.chat = new FakeChatHost(options.messages);
    this.memories = new FakeMemoriesHost(options.memories);
    this.memories.storedWorkspaceDiagnostics =
      options.memoryWorkspaceDiagnostics ?? [];
    this.panels = new FakePanelsHost(options.panels, options.panelResults);
    this.proposals = new FakeProposalsHost(options);
    this.recall = new FakeRecallHost(options.duePrompts);
    this.todos = new FakeTodosHost(options.todos);
    this.triggers = new FakeTriggersHost(options.triggers);
    this.youtube = new FakeYouTubeHost(options.transcriptDecisions);
  }
}
