const MAX_RECORDING_MS = 30_000;
const SAMPLE_INTERVAL_MS = 50;
const SILENCE_LEVEL = 0.012;
const SILENCE_MS = 1_200;
const SPEECH_LEVEL = 0.02;

export type WatchForSpeechEnd = (
  stream: MediaStream,
  onSpeechEnd: () => void,
) => () => void;

function watchUntilDeadline(onSpeechEnd: () => void): () => void {
  const timer = window.setTimeout(onSpeechEnd, MAX_RECORDING_MS);
  return () => {
    window.clearTimeout(timer);
  };
}

/**
 * Watch microphone energy until speech has begun and trailing silence follows.
 * A hard deadline prevents an abandoned hands-free recording from running
 * forever. The returned cleanup never reports speech end.
 */
export const watchForSpeechEnd: WatchForSpeechEnd = (stream, onSpeechEnd) => {
  if (typeof AudioContext === "undefined") {
    return watchUntilDeadline(onSpeechEnd);
  }
  let context: AudioContext;
  try {
    context = new AudioContext();
  } catch {
    return watchUntilDeadline(onSpeechEnd);
  }
  const analyser = context.createAnalyser();
  const source = context.createMediaStreamSource(stream);
  analyser.fftSize = 1024;
  source.connect(analyser);
  void context.resume().catch(() => undefined);

  const samples = new Float32Array(analyser.fftSize);
  const startedAt = performance.now();
  let finished = false;
  let heardSpeech = false;
  let silenceStartedAt: number | null = null;

  const finish = (notify: boolean) => {
    if (finished) {
      return;
    }
    finished = true;
    window.clearInterval(timer);
    source.disconnect();
    void context.close().catch(() => undefined);
    if (notify) {
      onSpeechEnd();
    }
  };

  const timer = window.setInterval(() => {
    const now = performance.now();
    if (now - startedAt >= MAX_RECORDING_MS) {
      finish(true);
      return;
    }

    analyser.getFloatTimeDomainData(samples);
    let squaredTotal = 0;
    for (const sample of samples) {
      squaredTotal += sample * sample;
    }
    const level = Math.sqrt(squaredTotal / samples.length);

    if (!heardSpeech) {
      heardSpeech = level >= SPEECH_LEVEL;
      return;
    }
    if (level > SILENCE_LEVEL) {
      silenceStartedAt = null;
      return;
    }
    silenceStartedAt ??= now;
    if (now - silenceStartedAt >= SILENCE_MS) {
      finish(true);
    }
  }, SAMPLE_INTERVAL_MS);

  return () => {
    finish(false);
  };
};
