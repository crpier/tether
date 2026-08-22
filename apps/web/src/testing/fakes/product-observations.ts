import type {
  ProductObservation,
  ProductObservationsHost,
} from "../../host/product-observations";

export class FakeProductObservationsHost implements ProductObservationsHost {
  storedObservations: ProductObservation[];
  listCalls = 0;
  resolveCalls: { observationId: string; version: number }[] = [];

  constructor(observations: ProductObservation[] = []) {
    this.storedObservations = observations;
  }

  listProductObservations(): Promise<ProductObservation[]> {
    this.listCalls += 1;
    return Promise.resolve(
      this.storedObservations.filter(
        (observation) => observation.status === "open",
      ),
    );
  }

  resolveProductObservation(
    observationId: string,
    version: number,
  ): Promise<ProductObservation> {
    this.resolveCalls.push({ observationId, version });
    const observation = this.storedObservations.find(
      (candidate) => candidate.id === observationId,
    );
    if (observation === undefined) {
      return Promise.reject(new Error("Product observation not found"));
    }
    const resolved: ProductObservation = {
      ...observation,
      resolved_at: "2026-01-02T00:00:00Z",
      status: "resolved",
      updated_at: "2026-01-02T00:00:00Z",
      version: version + 1,
    };
    this.storedObservations = this.storedObservations.map((candidate) =>
      candidate.id === observationId ? resolved : candidate,
    );
    return Promise.resolve(resolved);
  }
}
