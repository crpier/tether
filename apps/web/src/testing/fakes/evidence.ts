import type { Evidence, EvidenceHost } from "../../host/evidence";

export class FakeEvidenceHost implements EvidenceHost {
  readonly evidenceByUri = new Map<string, Evidence>();

  constructor(evidence: Evidence[] = []) {
    for (const item of evidence) {
      this.evidenceByUri.set(item.uri, item);
    }
  }

  resolveEvidence(uri: string): Promise<Evidence> {
    const evidence = this.evidenceByUri.get(uri);
    return evidence === undefined
      ? Promise.reject(new Error("Evidence is unavailable"))
      : Promise.resolve(evidence);
  }
}
