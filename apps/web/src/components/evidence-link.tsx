import type { JSX } from "solid-js";

const messageReference = /^tether:\/\/message\/[0-9A-Za-z-]+$/;
const healthReference =
  /^tether:\/\/health-connect\/(?:exercise|sleep)\/[0-9A-Za-z-]+@v[1-9][0-9]*$/;
const embeddedReference =
  /tether:\/\/(?:message\/[0-9A-Za-z-]+|health-connect\/(?:exercise|sleep)\/[0-9A-Za-z-]+@v[1-9][0-9]*)/g;

export interface EvidenceTextPart {
  evidence: boolean;
  text: string;
}

export function evidenceTextParts(value: string): EvidenceTextPart[] {
  const parts: EvidenceTextPart[] = [];
  let start = 0;
  for (const match of value.matchAll(embeddedReference)) {
    const index = match.index;
    if (index > start) {
      parts.push({ evidence: false, text: value.slice(start, index) });
    }
    parts.push({ evidence: true, text: match[0] });
    start = index + match[0].length;
  }
  if (start < value.length) {
    parts.push({ evidence: false, text: value.slice(start) });
  }
  return parts;
}

export function isEvidenceUri(value: string): boolean {
  return messageReference.test(value) || healthReference.test(value);
}

export function EvidenceLink(props: {
  children: JSX.Element;
  class?: string;
  onOpen: (uri: string) => void;
  uri: string;
}) {
  return (
    <button
      class={props.class ?? "underline underline-offset-2"}
      onClick={() => {
        props.onOpen(props.uri);
      }}
      title={props.uri}
      type="button"
    >
      {props.children}
    </button>
  );
}
