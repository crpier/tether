import type {
  DreamingHost,
  DreamRun,
  DreamRunDetail,
} from "../../host/dreaming";
import { ApiError } from "../../host/error";

export class FakeDreamingHost implements DreamingHost {
  listDreamRunsCalls = 0;
  getDreamRunCalls: string[] = [];
  dreamNowCalls = 0;

  constructor(
    public storedRuns: DreamRun[] = [],
    public storedDetails: Partial<Record<string, DreamRunDetail>> = {},
    /** Runs handed back from dreamNow; also appended to storedRuns. */
    public queuedRuns: DreamRun[] = [],
  ) {}

  listDreamRuns(): Promise<DreamRun[]> {
    this.listDreamRunsCalls += 1;
    return Promise.resolve(this.storedRuns);
  }

  dreamNow(): Promise<DreamRun[]> {
    this.dreamNowCalls += 1;
    this.storedRuns = [...this.queuedRuns, ...this.storedRuns];
    return Promise.resolve(this.queuedRuns);
  }

  getDreamRun(runId: string): Promise<DreamRunDetail> {
    this.getDreamRunCalls.push(runId);
    const detail = this.storedDetails[runId];
    return detail === undefined
      ? Promise.reject(new ApiError(404))
      : Promise.resolve(detail);
  }
}
