import { createArtifactsHost, type ArtifactsHost } from "./artifacts";
import { createAuthHost, type AuthHost } from "./auth";
import { createBucketHost, type BucketHost } from "./bucket";
import { createChatHost, type ChatHost } from "./chat";
import { createMemoriesHost, type MemoriesHost } from "./memories";
import {
  createNotificationsHost,
  type NotificationsHost,
} from "./notifications";
import { createPanelsHost, type PanelsHost } from "./panels";
import { createProposalsHost, type ProposalsHost } from "./proposals";
import { createProviderAuthHost, type ProviderAuthHost } from "./provider-auth";
import { createPushHost, type PushHost } from "./push";
import { createRecallHost, type RecallHost } from "./recall";
import { createTodosHost, type TodosHost } from "./todos";
import { createRestContext, type RestHostDependencies } from "./transport";
import { createTriggersHost, type TriggersHost } from "./triggers";
import { createGmailHost, type GmailHost } from "./gmail";
import { createYouTubeHost, type YouTubeHost } from "./youtube";

export * from "./artifacts";
export * from "./auth";
export * from "./bucket";
export * from "./chat";
export * from "./memories";
export * from "./notifications";
export * from "./panels";
export * from "./proposals";
export * from "./provider-auth";
export * from "./push";
export * from "./recall";
export * from "./todos";
export { ApiError } from "./error";
export type { RestHostDependencies } from "./transport";
export * from "./triggers";
export * from "./gmail";
export * from "./youtube";

export interface WebHost {
  artifacts: ArtifactsHost;
  auth: AuthHost;
  bucket: BucketHost;
  chat: ChatHost;
  memories: MemoriesHost;
  notifications: NotificationsHost;
  panels: PanelsHost;
  proposals: ProposalsHost;
  providerAuth: ProviderAuthHost;
  push: PushHost;
  recall: RecallHost;
  todos: TodosHost;
  triggers: TriggersHost;
  gmail: GmailHost;
  youtube: YouTubeHost;
}

export function createRestHost(
  dependencies: RestHostDependencies = {},
): WebHost {
  const context = createRestContext(dependencies);
  return {
    artifacts: createArtifactsHost(context),
    auth: createAuthHost(context),
    bucket: createBucketHost(context),
    chat: createChatHost(context),
    memories: createMemoriesHost(context),
    notifications: createNotificationsHost(context),
    panels: createPanelsHost(context),
    proposals: createProposalsHost(context),
    providerAuth: createProviderAuthHost(context),
    push: createPushHost(context),
    recall: createRecallHost(context),
    todos: createTodosHost(context),
    triggers: createTriggersHost(context),
    gmail: createGmailHost(context),
    youtube: createYouTubeHost(context),
  };
}
