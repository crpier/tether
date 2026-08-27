import type { HealthHost, HealthOverview } from "../../host";

export class FakeHealthHost implements HealthHost {
  readonly overview: HealthOverview | undefined;

  constructor(overview?: HealthOverview) {
    this.overview = overview;
  }

  getOverview(days = 7): Promise<HealthOverview> {
    void days;
    if (this.overview === undefined) {
      return Promise.reject(new Error("No fake Health overview configured"));
    }
    return Promise.resolve(this.overview);
  }
}
