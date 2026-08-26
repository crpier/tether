import { cleanup, render, screen, waitFor } from "@solidjs/testing-library";
import { createSignal } from "solid-js";
import { afterEach, expect, test, vi } from "vitest";

import { VoiceComposerControls } from "./voice-composer";

class FakeMediaRecorder {
  static instances: FakeMediaRecorder[] = [];
  ondataavailable: ((event: { data: Blob }) => void) | null = null;
  onstop: (() => void) | null = null;

  constructor() {
    FakeMediaRecorder.instances.push(this);
  }

  start(): void {
    // No browser audio in this public-interface test.
  }

  stop(): void {
    this.ondataavailable?.({ data: new Blob(["voice"]) });
    this.onstop?.();
  }
}

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

function stubMicrophone(): void {
  FakeMediaRecorder.instances = [];
  vi.stubGlobal("MediaRecorder", FakeMediaRecorder);
  Object.defineProperty(navigator, "mediaDevices", {
    configurable: true,
    value: {
      getUserMedia: () =>
        Promise.resolve({ getTracks: () => [] } as unknown as MediaStream),
    },
  });
}

test("listening starts after its cue and stopping capture emits the stop cue", async () => {
  stubMicrophone();
  let finishStartCue: (() => void) | undefined;
  const startCue = new Promise<void>((resolve) => {
    finishStartCue = resolve;
  });
  const stopped = vi.fn();
  const [active] = createSignal(true);
  const [autoStart] = createSignal(1);

  render(() => (
    <VoiceComposerControls
      active={active}
      autoStartSignal={autoStart}
      onEndConversation={() => undefined}
      onRecordingStart={() => startCue}
      onRecordingStop={stopped}
      onStartConversation={() => undefined}
      onTranscript={() => undefined}
      playbackState={() => "idle"}
      recordingCancelSignal={() => 0}
      transcribe={() => new Promise<string>(() => undefined)}
    />
  ));

  await Promise.resolve();
  expect(FakeMediaRecorder.instances).toHaveLength(0);

  finishStartCue?.();
  await screen.findByText("Listening…");
  expect(
    screen.getByRole("img", { name: "Microphone is listening" }),
  ).toBeInTheDocument();
  expect(FakeMediaRecorder.instances).toHaveLength(1);

  FakeMediaRecorder.instances[0]?.stop();
  await waitFor(() => expect(stopped).toHaveBeenCalledOnce());
});

test("voice playback uses Kitn speaking presentation", async () => {
  stubMicrophone();
  const [active] = createSignal(true);
  const [playbackState, setPlaybackState] = createSignal<
    "idle" | "playing" | "error"
  >("idle");

  render(() => (
    <VoiceComposerControls
      active={active}
      autoStartSignal={() => 0}
      onEndConversation={() => undefined}
      onRecordingStart={() => Promise.resolve()}
      onRecordingStop={() => undefined}
      onStartConversation={() => undefined}
      onTranscript={() => undefined}
      playbackState={playbackState}
      recordingCancelSignal={() => 0}
      transcribe={() => Promise.resolve("")}
    />
  ));

  setPlaybackState("playing");

  expect(await screen.findByText("Tether is speaking…")).toBeInTheDocument();
  expect(
    screen.getByRole("img", { name: "Tether is speaking" }),
  ).toBeInTheDocument();
});

test("a prompt cancels recording while its start cue is still playing", async () => {
  stubMicrophone();
  let finishStartCue: (() => void) | undefined;
  const startCue = new Promise<void>((resolve) => {
    finishStartCue = resolve;
  });
  const [active] = createSignal(true);
  const [recordingCancelSignal, setRecordingCancelSignal] = createSignal(0);

  render(() => (
    <VoiceComposerControls
      active={active}
      autoStartSignal={() => 1}
      onEndConversation={() => undefined}
      onRecordingStart={() => startCue}
      onRecordingStop={() => undefined}
      onStartConversation={() => undefined}
      onTranscript={() => undefined}
      playbackState={() => "idle"}
      recordingCancelSignal={recordingCancelSignal}
      transcribe={() => Promise.resolve("")}
    />
  ));

  await Promise.resolve();
  setRecordingCancelSignal(1);
  finishStartCue?.();
  await Promise.resolve();
  await Promise.resolve();

  expect(FakeMediaRecorder.instances).toHaveLength(0);
});
