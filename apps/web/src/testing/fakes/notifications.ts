import type { Notification, NotificationsHost } from "../../host/notifications";

export class FakeNotificationsHost implements NotificationsHost {
  dismissNotificationCalls: string[] = [];
  clearNotificationsCalls = 0;
  listNotificationsCalls = 0;
  storedNotifications: Notification[] = [];

  listNotifications(): Promise<Notification[]> {
    this.listNotificationsCalls += 1;
    return Promise.resolve(this.storedNotifications);
  }

  dismissNotification(notificationId: string): Promise<void> {
    this.dismissNotificationCalls.push(notificationId);
    this.storedNotifications = this.storedNotifications.filter(
      (item) => item.id !== notificationId,
    );
    return Promise.resolve();
  }

  clearNotifications(): Promise<void> {
    this.clearNotificationsCalls += 1;
    this.storedNotifications = [];
    return Promise.resolve();
  }
}
