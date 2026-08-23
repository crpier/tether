import type {
  ProductObservation,
  ProductObservationsHost,
} from "../../host/product-observations";

export class FakeProductObservationsHost implements ProductObservationsHost {
  storedObservations: ProductObservation[];
  listCalls = 0;
  recordCalls: {
    conversationId: string;
    interpretation: string;
    messageId: string;
  }[] = [];
  resolveCalls: { observationId: string; version: number }[] = [];

  constructor(observations: ProductObservation[] = []) {
    this.storedObservations = observations;
  }

  recordProductObservation(
    conversationId: string,
    messageId: string,
    interpretation: string,
  ): Promise<ProductObservation> {
    this.recordCalls.push({ conversationId, interpretation, messageId });
    const now = "2026-01-02T00:00:00Z";
    const observation: ProductObservation = {
      conversation_id: conversationId,
      created_at: now,
      id: "018f0000-0000-7000-8000-0000000000f1",
      interpretation,
      message_id: messageId,
      resolved_at: null,
      status: "open",
      updated_at: now,
      version: 1,
      wording: "Server-owned source wording",
    };
    this.storedObservations = [observation, ...this.storedObservations];
    return Promise.resolve(observation);
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
