import type {
  ProviderAuthHost,
  ProviderAuthStatus,
} from "../../host/provider-auth";

export class FakeProviderAuthHost implements ProviderAuthHost {
  cancelProviderAuthCalls = 0;
  nextProviderAuthStatus: ProviderAuthStatus | undefined;
  providerAuthStatus: ProviderAuthStatus = {
    error: null,
    expires_in_seconds: null,
    state: "connected",
    user_code: null,
    verification_uri: null,
  };

  getProviderAuthStatus() {
    return Promise.resolve(this.providerAuthStatus);
  }

  startProviderAuth() {
    if (this.nextProviderAuthStatus !== undefined) {
      this.providerAuthStatus = this.nextProviderAuthStatus;
      this.nextProviderAuthStatus = undefined;
    }
    return Promise.resolve(this.providerAuthStatus);
  }

  cancelProviderAuth() {
    this.cancelProviderAuthCalls += 1;
    this.providerAuthStatus = {
      error: null,
      expires_in_seconds: null,
      state: "disconnected",
      user_code: null,
      verification_uri: null,
    };
    return Promise.resolve(this.providerAuthStatus);
  }
}
