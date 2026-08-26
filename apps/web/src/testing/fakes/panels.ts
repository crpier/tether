import type {
  CreatePanel,
  Panel,
  PanelResults,
  PanelsHost,
  UpdatePanel,
} from "../../host/panels";
import { ApiError } from "../../host/error";
import { panel } from "../fixtures";

export class FakePanelsHost implements PanelsHost {
  storedPanels: Panel[];
  storedPanelResults: Record<string, PanelResults>;
  createPanelCalls: CreatePanel[] = [];
  updatePanelCalls: { body: UpdatePanel; panelId: string }[] = [];
  deletePanelCalls: { panelId: string; version: number }[] = [];
  deletePanelRejections: ApiError[] = [];

  constructor(
    panels: Panel[] = [],
    panelResults: Record<string, PanelResults> = {},
  ) {
    this.storedPanels = panels;
    this.storedPanelResults = panelResults;
  }

  listPanels(): Promise<Panel[]> {
    return Promise.resolve([...this.storedPanels]);
  }

  createPanel(body: CreatePanel): Promise<Panel> {
    this.createPanelCalls.push(body);
    const created = panel({
      columns: body.columns,
      facets: body.facets,
      name: body.name,
      position: body.position,
      query: body.query,
      render_kind: body.render_kind,
      vega_lite_spec: body.vega_lite_spec,
      window_days: body.window_days,
    });
    this.storedPanels = [...this.storedPanels, created];
    return Promise.resolve(created);
  }

  updatePanel(panelId: string, body: UpdatePanel): Promise<Panel> {
    this.updatePanelCalls.push({ body, panelId });
    const existing = this.storedPanels.find(
      (candidate) => candidate.id === panelId,
    );
    if (existing === undefined) {
      return Promise.reject(new ApiError(404));
    }
    const updated: Panel = {
      ...existing,
      name: body.name,
      version: existing.version + 1,
    };
    this.storedPanels = this.storedPanels.map((candidate) =>
      candidate.id === panelId ? updated : candidate,
    );
    return Promise.resolve(updated);
  }

  deletePanel(panelId: string, version: number): Promise<Panel> {
    this.deletePanelCalls.push({ panelId, version });
    const forced = this.deletePanelRejections.shift();
    if (forced !== undefined) {
      return Promise.reject(forced);
    }
    const existing = this.storedPanels.find(
      (candidate) => candidate.id === panelId,
    );
    if (existing === undefined) {
      return Promise.reject(new ApiError(404));
    }
    this.storedPanels = this.storedPanels.filter(
      (candidate) => candidate.id !== panelId,
    );
    return Promise.resolve(existing);
  }

  getPanelResults(panelId: string): Promise<PanelResults> {
    if (panelId in this.storedPanelResults) {
      return Promise.resolve(this.storedPanelResults[panelId]);
    }
    return Promise.resolve({ topics: [], total: 0 });
  }
}
