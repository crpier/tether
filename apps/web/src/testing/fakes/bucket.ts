import type {
  AddBucketItem,
  BucketHost,
  BucketItem,
  BucketItemAdded,
  BucketItemState,
  BucketTriageReport,
  DedupAdvisory,
} from "../../host/bucket";
import { ApiError } from "../../host/error";
import { bucketItem, emptyTriageReport } from "../fixtures";

export class FakeBucketHost implements BucketHost {
  storedBucketItems: BucketItem[];
  addBucketItemCalls: AddBucketItem[] = [];
  completeBucketItemCalls: { bucketItemId: string; version: number }[] = [];
  deleteBucketItemCalls: { bucketItemId: string; version: number }[] = [];
  searchBucketItemsCalls: string[] = [];
  listBucketItemsCalls = 0;
  serverBucketItemVersions: Record<string, number> = {};
  addBucketItemRejections: ApiError[] = [];
  completeBucketItemRejections: ApiError[] = [];
  deleteBucketItemRejections: ApiError[] = [];
  nextDedup: DedupAdvisory = { duplicates: [], severity: "none" };
  triageReport: BucketTriageReport = emptyTriageReport;

  constructor(bucketItems: BucketItem[] = []) {
    this.storedBucketItems = bucketItems;
  }

  listBucketItems(state: BucketItemState): Promise<BucketItem[]> {
    this.listBucketItemsCalls += 1;
    return Promise.resolve(
      this.storedBucketItems.filter((item) => item.state === state),
    );
  }

  searchBucketItems(q: string): Promise<BucketItem[]> {
    this.searchBucketItemsCalls.push(q);
    const terms = q.toLowerCase().split(/\s+/).filter(Boolean);
    return Promise.resolve(
      this.storedBucketItems.filter(
        (item) =>
          item.state === "active" &&
          terms.every((term) => item.title.toLowerCase().includes(term)),
      ),
    );
  }

  addBucketItem(body: AddBucketItem): Promise<BucketItemAdded> {
    this.addBucketItemCalls.push(body);
    const forced = this.addBucketItemRejections.shift();
    if (forced !== undefined) {
      return Promise.reject(forced);
    }
    const data = body.data as Record<string, unknown>;
    const named = data.title ?? data.name ?? data.destination;
    const title = typeof named === "string" ? named : "untitled";
    const created = bucketItem({
      data: body.data,
      id: `018f0000-0000-7000-8000-0000000001${this.addBucketItemCalls.length
        .toString()
        .padStart(2, "0")}`,
      intent_context: body.intent_context,
      item_type: body.item_type,
      title,
    });
    this.storedBucketItems = [created, ...this.storedBucketItems];
    const dedup = this.nextDedup;
    this.nextDedup = { duplicates: [], severity: "none" };
    return Promise.resolve({ dedup, item: created });
  }

  completeBucketItem(
    bucketItemId: string,
    version: number,
  ): Promise<BucketItem> {
    this.completeBucketItemCalls.push({ bucketItemId, version });
    return this.terminateBucketItem(
      bucketItemId,
      version,
      "completed",
      this.completeBucketItemRejections,
    );
  }

  deleteBucketItem(bucketItemId: string, version: number): Promise<BucketItem> {
    this.deleteBucketItemCalls.push({ bucketItemId, version });
    return this.terminateBucketItem(
      bucketItemId,
      version,
      "deleted",
      this.deleteBucketItemRejections,
    );
  }

  getBucketTriage(): Promise<BucketTriageReport> {
    return Promise.resolve(this.triageReport);
  }

  private terminateBucketItem(
    bucketItemId: string,
    version: number,
    state: "completed" | "deleted",
    rejections: ApiError[],
  ): Promise<BucketItem> {
    const forced = rejections.shift();
    if (forced !== undefined) {
      return Promise.reject(forced);
    }
    const serverVersion = this.serverBucketItemVersions[bucketItemId];
    if (
      Object.hasOwn(this.serverBucketItemVersions, bucketItemId) &&
      serverVersion !== version
    ) {
      this.storedBucketItems = this.storedBucketItems.map((existing) =>
        existing.id === bucketItemId
          ? { ...existing, version: serverVersion }
          : existing,
      );
      return Promise.reject(new ApiError(409));
    }
    const current = this.storedBucketItems.find(
      (existing) => existing.id === bucketItemId,
    );
    if (current === undefined) {
      return Promise.reject(new ApiError(404));
    }
    if (current.state !== "active") {
      return Promise.reject(new ApiError(409));
    }
    const stamp = "2026-01-02T00:00:00Z";
    const terminal: BucketItem = {
      ...current,
      completed_at: state === "completed" ? stamp : current.completed_at,
      deleted_at: state === "deleted" ? stamp : current.deleted_at,
      state,
      updated_at: stamp,
      version: version + 1,
    };
    this.serverBucketItemVersions[bucketItemId] = terminal.version;
    this.storedBucketItems = this.storedBucketItems.map((existing) =>
      existing.id === bucketItemId ? terminal : existing,
    );
    return Promise.resolve(terminal);
  }
}
