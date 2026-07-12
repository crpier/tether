import { useQueryClient } from "@tanstack/solid-query";
import { Show, createSignal } from "solid-js";
import type { JSX } from "solid-js";

import type { TetherApi } from "./api";
import { queryKeys } from "./lib/query-keys";
import { Button } from "@/components/ui/button";
import {
  TextField,
  TextFieldInput,
  TextFieldLabel,
} from "@/components/ui/text-field";

export function LoginScreen(props: { api: TetherApi }) {
  const queryClient = useQueryClient();
  const [password, setPassword] = createSignal("");
  const [error, setError] = createSignal<string | undefined>();
  const [submitting, setSubmitting] = createSignal(false);

  const submit = async () => {
    setSubmitting(true);
    setError(undefined);
    try {
      await props.api.login(password());
      setPassword("");
      await queryClient.invalidateQueries({ queryKey: queryKeys.session });
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "Login failed");
    } finally {
      setSubmitting(false);
    }
  };

  const onSubmit: JSX.EventHandler<HTMLFormElement, SubmitEvent> = (event) => {
    event.preventDefault();
    void submit();
  };

  return (
    <main
      aria-labelledby="login-title"
      class="flex min-h-screen items-center justify-center p-6"
    >
      <div class="bg-card text-card-foreground w-full max-w-sm space-y-6 rounded-xl border p-8 shadow-sm">
        <h1 id="login-title" class="text-xl font-semibold tracking-tight">
          Sign in to Tether
        </h1>
        <form onSubmit={onSubmit} class="space-y-4">
          <TextField value={password()} onChange={setPassword}>
            <TextFieldLabel>Password</TextFieldLabel>
            <TextFieldInput
              autocomplete="current-password"
              name="password"
              type="password"
            />
          </TextField>
          <Button class="w-full" disabled={submitting()} type="submit">
            Log in
          </Button>
        </form>
        <Show when={error()}>
          {(message) => (
            <p class="text-destructive text-sm" role="alert">
              {message()}
            </p>
          )}
        </Show>
      </div>
    </main>
  );
}
