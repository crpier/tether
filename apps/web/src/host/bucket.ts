import type { components } from "../generated";
import { requireData, type RestContext } from "./transport";

export type BucketItem = components["schemas"]["BucketItemRead"];
export type BucketItemType = components["schemas"]["ItemType"];
export type BucketItemState = components["schemas"]["BucketItemState"];
export type AddBucketItem = components["schemas"]["AddBucketItemRequest"];
export type BucketItemAdded = components["schemas"]["AddBucketItemResponse"];
export type DedupAdvisory = components["schemas"]["DedupAdvisoryRead"];
export type BucketTriageReport = components["schemas"]["TriageReport"];

export interface BucketHost {
  listBucketItems(state: BucketItemState): Promise<BucketItem[]>;
  searchBucketItems(q: string): Promise<BucketItem[]>;
  addBucketItem(body: AddBucketItem): Promise<BucketItemAdded>;
  completeBucketItem(
    bucketItemId: string,
    version: number,
  ): Promise<BucketItem>;
  deleteBucketItem(bucketItemId: string, version: number): Promise<BucketItem>;
  getBucketTriage(): Promise<BucketTriageReport>;
}

export function createBucketHost(context: RestContext): BucketHost {
  return {
    async listBucketItems(state) {
      const { data, response } = await context.client.GET("/api/bucket-items", {
        params: { query: { state } },
      });
      return requireData(data, response);
    },
    async searchBucketItems(q) {
      const { data, response } = await context.client.GET(
        "/api/bucket-items/search",
        { params: { query: { q } } },
      );
      return requireData(data, response);
    },
    async addBucketItem(body) {
      const { data, response } = await context.client.POST(
        "/api/bucket-items",
        { body },
      );
      return requireData(data, response);
    },
    async completeBucketItem(bucketItemId, version) {
      const { data, response } = await context.client.POST(
        "/api/bucket-items/{bucket_item_id}/complete",
        {
          body: { version },
          params: { path: { bucket_item_id: bucketItemId } },
        },
      );
      return requireData(data, response);
    },
    async deleteBucketItem(bucketItemId, version) {
      const { data, response } = await context.client.DELETE(
        "/api/bucket-items/{bucket_item_id}",
        {
          params: {
            path: { bucket_item_id: bucketItemId },
            query: { version },
          },
        },
      );
      return requireData(data, response);
    },
    async getBucketTriage() {
      const { data, response } = await context.client.GET(
        "/api/bucket-items/triage",
      );
      return requireData(data, response);
    },
  };
}
