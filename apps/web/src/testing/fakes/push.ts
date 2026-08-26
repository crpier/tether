import type { PushHost, PushStatus } from "../../host/push";

export class FakePushHost implements PushHost {
  pushSubscribed = false;
  subscribeCalls: { auth: string; endpoint: string; p256dh: string }[] = [];
  unsubscribeCalls: string[] = [];

  getPushConfig() {
    return Promise.resolve({
      vapid_public_key: "BBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB",
    });
  }

  getPushStatus(): Promise<PushStatus> {
    return Promise.resolve({
      count: this.pushSubscribed ? 1 : 0,
      subscribed: this.pushSubscribed,
    });
  }

  subscribePush(endpoint: string, p256dh: string, auth: string) {
    this.subscribeCalls.push({ auth, endpoint, p256dh });
    this.pushSubscribed = true;
    return Promise.resolve();
  }

  unsubscribePush(endpoint: string): Promise<PushStatus> {
    this.unsubscribeCalls.push(endpoint);
    this.pushSubscribed = false;
    return Promise.resolve({ count: 0, subscribed: false });
  }
}
