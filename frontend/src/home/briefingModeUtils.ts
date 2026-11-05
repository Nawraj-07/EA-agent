/** Smart briefing mode filter helpers (Home Page). */

export type BriefingMode = "morning" | "evening" | "weekly" | "monday";

export const DEFAULT_BRIEFING_TIMEZONE = "Asia/Kolkata";

export const MODE_FILTERS: { id: BriefingMode; label: string }[] = [
  { id: "morning", label: "Morning" },
  { id: "evening", label: "Evening" },
  { id: "weekly", label: "Weekly" },
  { id: "monday", label: "Monday" },
];

/**
 * True when `when` falls on Monday in the given IANA timezone.
 * Uses the configured zone — not the browser's default timezone alone.
 */
export function isMondayInTimezone(
  timeZone: string,
  when: Date = new Date()
): boolean {
  const tz = (timeZone || DEFAULT_BRIEFING_TIMEZONE).trim() || DEFAULT_BRIEFING_TIMEZONE;
  try {
    const weekday = new Intl.DateTimeFormat("en-US", {
      timeZone: tz,
      weekday: "short",
    }).format(when);
    return weekday === "Mon";
  } catch {
    // Invalid IANA id — fall back to Asia/Kolkata, never silent browser TZ.
    const weekday = new Intl.DateTimeFormat("en-US", {
      timeZone: DEFAULT_BRIEFING_TIMEZONE,
      weekday: "short",
    }).format(when);
    return weekday === "Mon";
  }
}

export function resolveBriefingTimezone(configured?: string | null): string {
  const value = (configured || "").trim();
  return value || DEFAULT_BRIEFING_TIMEZONE;
}
