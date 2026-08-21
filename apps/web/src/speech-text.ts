/**
 * Deterministic Markdown → spoken-text normalization.
 *
 * Model guidance is the semantic solution for listening-oriented answers;
 * this module is the compliance backstop that degrades safely when the model
 * still emits screen-oriented markup. It never forks visual rendering:
 * `MessageContent` remains the canonical Markdown surface.
 */

const FENCE_OPEN = /^\s*(`{3,}|~{3,})\s*(\S*)/;
const TABLE_SEPARATOR = /^\s*\|?\s*:?-+\s*(\|\s*:?-+\s*)*\|?\s*$/;
const HEADING = /^\s{0,3}#{1,6}\s+/;
const LIST_MARKER = /^\s*([*+-]|\d{1,9}[.)])\s+/;
const BLOCKQUOTE = /^\s{0,3}>\s?/;
const HORIZONTAL_RULE = /^\s{0,3}((-\s*){3,}|(\*\s*){3,}|(_\s*){3,})$/;
const IMAGE = /!\[[^\]]*\]\([^)]*\)/g;
const LINK = /\[([^\]]+)\]\([^)]*\)/g;
const BARE_URL = /https?:\/\/\S+/g;
const INLINE_EMPHASIS = /(\*\*\*|\*\*|\*|___|__|_|~~|`)/g;

function stripInlineMarkup(line: string): string {
  return line
    .replace(IMAGE, " ")
    .replace(LINK, "$1")
    .replace(BARE_URL, " ")
    .replace(INLINE_EMPHASIS, "")
    .replace(/\s{2,}/g, " ")
    .trim();
}

/**
 * Convert one settled assistant Markdown message into safe spoken text.
 * Returns "" when nothing speakable remains (blank or markup-only output).
 */
export function toSpeechText(markdown: string): string {
  const spoken: string[] = [];
  let fenceDelimiter: string | null = null;

  for (const line of markdown.split("\n")) {
    if (fenceDelimiter !== null) {
      if (line.trim().startsWith(fenceDelimiter)) {
        fenceDelimiter = null;
      }
      continue;
    }
    const open = FENCE_OPEN.exec(line);
    if (open !== null) {
      // Code and widget/artifact payloads are never read aloud; surrounding
      // explanation is kept.
      fenceDelimiter = open[1];
      continue;
    }
    if (HORIZONTAL_RULE.test(line)) {
      continue;
    }
    if (TABLE_SEPARATOR.test(line)) {
      continue;
    }
    let text = line.replace(HEADING, "");
    if (line.includes("|") && line.trim().startsWith("|")) {
      // Degrade safely: cells become a comma-separated line, never raw pipes.
      text = text
        .trim()
        .replace(/^\||\|$/g, "")
        .split("|")
        .map((cell) => cell.trim())
        .join(", ");
    }
    text = text.replace(BLOCKQUOTE, "").replace(LIST_MARKER, "");
    spoken.push(text.length === 0 ? "" : stripInlineMarkup(text));
  }

  return spoken
    .join("\n")
    .replace(/\n{3,}/g, "\n\n")
    .trim();
}
