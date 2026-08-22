import type {
  BucketItem,
  DuePrompt,
  DreamRun,
  DreamRunDetail,
  Evidence,
  Grant,
  GrantSuggestion,
  MemoryTopic,
  Message,
  MemoryWorkspaceDiagnostic,
  Panel,
  PanelResults,
  ProductObservation,
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
import { FakeDreamingHost } from "./fakes/dreaming";
import { FakeEvidenceHost } from "./fakes/evidence";
import { FakeMemoriesHost } from "./fakes/memories";
import { FakeNotificationsHost } from "./fakes/notifications";
import { FakePanelsHost } from "./fakes/panels";
import { FakeProductObservationsHost } from "./fakes/product-observations";
import { FakeProposalsHost } from "./fakes/proposals";
import { FakeProviderAuthHost } from "./fakes/provider-auth";
import { FakeGmailHost } from "./fakes/gmail";
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
  readonly dreaming: FakeDreamingHost;
  readonly evidence: FakeEvidenceHost;
  readonly memories: FakeMemoriesHost;
  readonly notifications = new FakeNotificationsHost();
  readonly panels: FakePanelsHost;
  readonly productObservations: FakeProductObservationsHost;
  readonly proposals: FakeProposalsHost;
  readonly providerAuth = new FakeProviderAuthHost();
  readonly gmail: FakeGmailHost;
  readonly push = new FakePushHost();
  readonly recall: FakeRecallHost;
  readonly todos: FakeTodosHost;
  readonly triggers: FakeTriggersHost;
  readonly youtube: FakeYouTubeHost;

  constructor(options: {
    authenticated: boolean;
    bucketItems?: BucketItem[];
    duePrompts?: DuePrompt[];
    dreamRunDetails?: Record<string, DreamRunDetail>;
    dreamNowRuns?: DreamRun[];
    dreamRuns?: DreamRun[];
    evidence?: Evidence[];
    grants?: Grant[];
    grantSuggestions?: GrantSuggestion[];
    memoryTopics?: MemoryTopic[];
    memoryWorkspaceDiagnostics?: MemoryWorkspaceDiagnostic[];
    messages?: Message[];
    panelResults?: Record<string, PanelResults>;
    panels?: Panel[];
    productObservations?: ProductObservation[];
    proposals?: Proposal[];
    todos?: Todo[];
    transcriptDecisions?: TranscriptDecision[];
    triggers?: Trigger[];
  }) {
    this.auth = new FakeAuthHost(options.authenticated);
    this.bucket = new FakeBucketHost(options.bucketItems);
    this.chat = new FakeChatHost(options.messages);
    this.dreaming = new FakeDreamingHost(
      options.dreamRuns,
      options.dreamRunDetails,
      options.dreamNowRuns,
    );
    this.evidence = new FakeEvidenceHost(options.evidence);
    this.memories = new FakeMemoriesHost(options.memoryTopics);
    this.memories.workspaceDiagnostics =
      options.memoryWorkspaceDiagnostics ?? [];
    this.panels = new FakePanelsHost(options.panels, options.panelResults);
    this.productObservations = new FakeProductObservationsHost(
      options.productObservations,
    );
    this.proposals = new FakeProposalsHost(options);
    this.recall = new FakeRecallHost(options.duePrompts);
    this.gmail = new FakeGmailHost();
    this.todos = new FakeTodosHost(options.todos);
    this.triggers = new FakeTriggersHost(options.triggers);
    this.youtube = new FakeYouTubeHost(options.transcriptDecisions);
  }
}
