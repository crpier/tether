import type { AuthHost } from "../../host/auth";

export class FakeAuthHost implements AuthHost {
  authenticated: boolean;
  loginPassword: string | undefined;

  constructor(authenticated: boolean) {
    this.authenticated = authenticated;
  }

  getSession() {
    return Promise.resolve({ authenticated: this.authenticated });
  }

  login(password: string) {
    this.loginPassword = password;
    this.authenticated = true;
    return Promise.resolve();
  }

  logout() {
    this.authenticated = false;
    return Promise.resolve();
  }
}
