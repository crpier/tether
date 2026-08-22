import { A } from "@solidjs/router";
import { createQuery, useQueryClient } from "@tanstack/solid-query";
import { For, Show, createSignal } from "solid-js";

import type {
  ProductObservation,
  ProductObservationsHost,
} from "../host/product-observations";
import { formatDateTime } from "../lib/format";
import { panelClass } from "../lib/panel";
import { queryKeys } from "../lib/query-keys";
import { Button } from "@/components/ui/button";

export function ProductObservationsPanel(props: {
  api: ProductObservationsHost;
}) {
  const queryClient = useQueryClient();
  const [error, setError] = createSignal<string | undefined>();
  const observationsQuery = createQuery(() => ({
    queryFn: () => props.api.listProductObservations(),
    queryKey: queryKeys.productObservations,
  }));

  const resolve = (observation: ProductObservation) => {
    void (async () => {
      setError(undefined);
      try {
        await props.api.resolveProductObservation(
          observation.id,
          observation.version,
        );
        await queryClient.refetchQueries({
          queryKey: queryKeys.productObservations,
        });
      } catch (caught) {
        setError(
          caught instanceof Error
            ? caught.message
            : "Could not resolve the product observation",
        );
      }
    })();
  };

  return (
    <section aria-label="Feedback" class={panelClass}>
      <div class="mb-3 flex items-center justify-between">
        <h2 class="text-sm font-semibold">Feedback</h2>
      </div>
      <Show
        fallback={
          <div class="space-y-2">
            <p class="text-sm font-medium">No open product observations</p>
            <p class="text-muted-foreground text-sm">
              Explicit feedback recorded through Chat appears here.
            </p>
            <A
              class="text-primary text-sm font-medium underline-offset-4 hover:underline"
              href={`/?prompt=${encodeURIComponent("Log this as product feedback: ")}`}
            >
              Record in Chat
            </A>
          </div>
        }
        when={(observationsQuery.data?.length ?? 0) > 0}
      >
        <ul class="space-y-2">
          <For each={observationsQuery.data ?? []}>
            {(observation) => (
              <li
                aria-label={`Product observation: ${observation.interpretation}`}
                class="bg-muted rounded-md border px-3 py-2 text-sm"
              >
                <div class="flex items-start gap-2">
                  <div class="min-w-0 flex-1">
                    <p class="font-medium">{observation.interpretation}</p>
                    <blockquote class="text-muted-foreground mt-1 border-l-2 pl-2 text-xs">
                      {observation.wording}
                    </blockquote>
                    <p class="text-muted-foreground mt-1 text-xs">
                      {formatDateTime(new Date(observation.created_at))}
                    </p>
                  </div>
                  <Button
                    onClick={() => {
                      resolve(observation);
                    }}
                    size="sm"
                    type="button"
                    variant="ghost"
                  >
                    Resolve
                  </Button>
                </div>
              </li>
            )}
          </For>
        </ul>
      </Show>
      <Show when={error()}>
        {(message) => (
          <p class="text-destructive mt-2 text-sm" role="alert">
            {message()}
          </p>
        )}
      </Show>
    </section>
  );
}
