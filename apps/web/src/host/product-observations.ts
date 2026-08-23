import type { components } from "../generated";
import { requireData, type RestContext } from "./transport";

export type ProductObservation =
  components["schemas"]["ProductObservationRead"];

export interface ProductObservationsHost {
  recordProductObservation(
    conversationId: string,
    messageId: string,
    interpretation: string,
  ): Promise<ProductObservation>;
  listProductObservations(): Promise<ProductObservation[]>;
  resolveProductObservation(
    observationId: string,
    version: number,
  ): Promise<ProductObservation>;
}

export function createProductObservationsHost(
  context: RestContext,
): ProductObservationsHost {
  return {
    async recordProductObservation(conversationId, messageId, interpretation) {
      const { data, response } = await context.client.POST(
        "/api/product-observations",
        {
          body: {
            conversation_id: conversationId,
            interpretation,
            message_id: messageId,
          },
        },
      );
      return requireData(data, response);
    },
    async listProductObservations() {
      const { data, response } = await context.client.GET(
        "/api/product-observations",
      );
      return requireData(data, response);
    },
    async resolveProductObservation(observationId, version) {
      const { data, response } = await context.client.POST(
        "/api/product-observations/{observation_id}/resolve",
        {
          body: { version },
          params: { path: { observation_id: observationId } },
        },
      );
      return requireData(data, response);
    },
  };
}
