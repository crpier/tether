import type { components } from "../generated";
import { requireData, type RestContext } from "./transport";

export type Panel = components["schemas"]["PanelRead"];
export type PanelResults = components["schemas"]["PanelResultsRead"];
export type CreatePanel = components["schemas"]["CreatePanelRequest"];
export type UpdatePanel = components["schemas"]["UpdatePanelRequest"];

export interface PanelsHost {
  listPanels(): Promise<Panel[]>;
  createPanel(body: CreatePanel): Promise<Panel>;
  updatePanel(panelId: string, body: UpdatePanel): Promise<Panel>;
  deletePanel(panelId: string, version: number): Promise<Panel>;
  getPanelResults(panelId: string, limit?: number): Promise<PanelResults>;
}

export function createPanelsHost(context: RestContext): PanelsHost {
  return {
    async listPanels() {
      const { data, response } = await context.client.GET("/api/panels");
      return requireData(data, response);
    },
    async createPanel(body) {
      const { data, response } = await context.client.POST("/api/panels", {
        body,
      });
      return requireData(data, response);
    },
    async updatePanel(panelId, body) {
      const { data, response } = await context.client.PUT(
        "/api/panels/{panel_id}",
        { body, params: { path: { panel_id: panelId } } },
      );
      return requireData(data, response);
    },
    async deletePanel(panelId, version) {
      const { data, response } = await context.client.DELETE(
        "/api/panels/{panel_id}",
        {
          params: { path: { panel_id: panelId }, query: { version } },
        },
      );
      return requireData(data, response);
    },
    async getPanelResults(panelId, limit) {
      const { data, response } = await context.client.GET(
        "/api/panels/{panel_id}/results",
        {
          params: {
            path: { panel_id: panelId },
            query: limit === undefined ? {} : { limit },
          },
        },
      );
      return requireData(data, response);
    },
  };
}
