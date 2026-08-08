import { A, useLocation } from "@solidjs/router";

export function NotFoundPage() {
  const location = useLocation();

  return (
    <section
      aria-labelledby="not-found-title"
      class="flex min-h-full flex-1 items-center justify-center p-6"
    >
      <div class="border-border bg-card text-card-foreground max-w-xl rounded-lg border p-6 shadow-sm">
        <p class="text-muted-foreground text-sm font-medium">404</p>
        <h1 class="mt-2 text-2xl font-semibold" id="not-found-title">
          Page not found
        </h1>
        <p class="text-muted-foreground mt-3">
          No Tether page exists at <code>{location.pathname}</code>.
        </p>
        <A
          class="bg-primary text-primary-foreground hover:bg-primary/90 mt-5 inline-flex rounded-md px-4 py-2 text-sm font-medium"
          href="/"
        >
          Go to Chat
        </A>
      </div>
    </section>
  );
}
