import type { components } from "../generated";
import { requireData, requireOk, type RestContext } from "./transport";

export type Notification = components["schemas"]["NotificationRead"];

export interface NotificationsHost {
  listNotifications(): Promise<Notification[]>;
  dismissNotification(notificationId: string): Promise<void>;
  clearNotifications(): Promise<void>;
}

export function createNotificationsHost(
  context: RestContext,
): NotificationsHost {
  return {
    async listNotifications() {
      const { data, response } = await context.client.GET("/api/notifications");
      return requireData(data, response);
    },
    async dismissNotification(notificationId) {
      const { response } = await context.client.DELETE(
        "/api/notifications/{notification_id}",
        { params: { path: { notification_id: notificationId } } },
      );
      requireOk(response);
    },
    async clearNotifications() {
      const { response } = await context.client.DELETE("/api/notifications");
      requireOk(response);
    },
  };
}
