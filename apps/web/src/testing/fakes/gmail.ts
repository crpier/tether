import type { GmailAuthStatus, GmailHost } from "../../host/gmail";

export class FakeGmailHost implements GmailHost {
  gmailAuthStatus: GmailAuthStatus = {
    authorization_url: null,
    error: null,
    state: "disconnected",
  };
  nextGmailAuthStatus: GmailAuthStatus | null = null;
  startGmailAuthCalls = 0;

  getGmailAuthStatus(): Promise<GmailAuthStatus> {
    return Promise.resolve(this.gmailAuthStatus);
  }

  startGmailAuth(): Promise<GmailAuthStatus> {
    this.startGmailAuthCalls += 1;
    if (this.nextGmailAuthStatus !== null) {
      this.gmailAuthStatus = this.nextGmailAuthStatus;
      this.nextGmailAuthStatus = null;
    }
    return Promise.resolve(this.gmailAuthStatus);
  }
}
