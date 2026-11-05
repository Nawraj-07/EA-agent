const MEET_LINK_RE =
  /https?:\/\/(?:meet\.google\.com|zoom\.us|teams\.microsoft\.com)[^\s<>)"']+/gi;

export function extractMeetingUrl(...parts: Array<string | null | undefined>): string | null {
  const blob = parts.filter(Boolean).join("\n");
  if (!blob) return null;
  const match = blob.match(MEET_LINK_RE);
  if (!match?.[0]) return null;
  return match[0].replace(/[).,;]+$/g, "");
}

export function isMeetingUrl(value?: string | null): boolean {
  if (!value) return false;
  return /^https?:\/\/(meet\.google\.com|zoom\.us|teams\.microsoft\.com)/i.test(
    value.trim()
  );
}
