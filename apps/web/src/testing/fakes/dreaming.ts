import type {
  DreamingHost,
  DreamRun,
  DreamRunDetail,
} from "../../host/dreaming";
import { ApiError } from "../../host/error";

export class FakeDreamingHost implements DreamingHost {
  listDreamRunsCalls = 0;
  getDreamRunCalls: string[] = [];

  constructor(
    public storedRuns: DreamRun[] = [],
    public storedDetails: Partial<Record<string, DreamRunDetail>> = {},
  ) {}

  listDreamRuns(): Promise<DreamRun[]> {
    this.listDreamRunsCalls += 1;
    return Promise.resolve(this.storedRuns);
  }

  getDreamRun(runId: string): Promise<DreamRunDetail> {
    this.getDreamRunCalls.push(runId);
    const detail = this.storedDetails[runId];
    return detail === undefined
      ? Promise.reject(new ApiError(404))
      : Promise.resolve(detail);
  }
}
