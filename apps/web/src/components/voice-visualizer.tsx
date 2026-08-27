import { AudioVisualizer } from "@kitn.ai/ui/solid";

export function VoiceVisualizer(props: {
  class?: string;
  label: string;
  state: "listening" | "speaking";
}) {
  return (
    <AudioVisualizer
      class={props.class}
      color="currentColor"
      label={props.label}
      size="icon"
      state={props.state}
      variant="bar"
    />
  );
}
