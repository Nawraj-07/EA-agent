export type BriefingPriority = "high" | "medium" | "low";

export type StructuredBriefingItem = {
  title: string;
  description?: string;
  priority?: BriefingPriority | string;
  recommended_action?: string;
  source_email_id?: string | null;
  sender?: string;
  recipient?: string;
  subject?: string;
  received_at?: string;
  kind?: string;
  followup_state?: string;
  followup_confidence?: string;
  followup_reason?: string;
  gmail_thread_id?: string | null;
  recommendation_key?: string;
  recommendation_id?: string;
  followup_actions_enabled?: boolean;
  waiting_hours?: number;
  followup_attempt_count?: number;
  evidence_status?: string;
};

export type StructuredMeeting = {
  event_title: string;
  event_date?: string;
  start_time?: string;
  end_time?: string;
  location?: string;
  status?: string;
  source_email_id?: string | null;
  organizer?: string;
  sender?: string;
  subject?: string;
  received_at?: string;
};

export type StructuredDeadline = {
  title: string;
  due_date?: string;
  priority?: string;
  recommended_action?: string;
  source_email_id?: string | null;
  sender?: string;
  subject?: string;
  received_at?: string;
  overdue?: boolean;
};

export type BriefingOverview = {
  important_updates?: number;
  pending_actions?: number;
  meetings?: number;
  text?: string;
};

export type BriefingContent = {
  headline?: string;
  highlights?: string[];
  deadlines?: string[];
  action_items?: string[];
  notes?: string;
  items?: StructuredBriefingItem[];
  meetings?: StructuredMeeting[];
  upcoming_deadlines?: StructuredDeadline[];
  overview?: BriefingOverview;
  email_count?: number;
};

export type NormalizedBriefing = {
  items: StructuredBriefingItem[];
  meetings: StructuredMeeting[];
  deadlines: StructuredDeadline[];
  importantUpdates: number;
  pendingActions: number;
  meetingCount: number;
  notes: string;
  structured: boolean;
};

function storedCount(value: number | undefined, fallback: number): number {
  return typeof value === "number" && Number.isFinite(value) ? value : fallback;
}

function asItemsFromLegacy(content: BriefingContent): StructuredBriefingItem[] {
  const items: StructuredBriefingItem[] = [];
  for (const text of content.action_items ?? []) {
    if (!text.trim()) continue;
    items.push({
      title: text,
      description: text,
      priority: "medium",
      recommended_action: text,
      kind: "action",
    });
  }
  for (const text of content.highlights ?? []) {
    if (!text.trim()) continue;
    items.push({
      title: text,
      description: text,
      priority: "medium",
      kind: "update",
    });
  }
  return items;
}

export function normalizeBriefing(content?: BriefingContent | null): NormalizedBriefing {
  const items = content?.items?.length
    ? content.items
    : asItemsFromLegacy(content ?? {});
  const deadlines = content?.upcoming_deadlines?.length
    ? content.upcoming_deadlines
    : (content?.deadlines ?? []).map((title) => ({ title, priority: "medium" }));
  const meetings = content?.meetings ?? [];
  const importantUpdates = storedCount(
    content?.overview?.important_updates,
    items.filter((item) => (item.priority || "").toLowerCase() === "high").length
  );
  const pendingActions = storedCount(
    content?.overview?.pending_actions,
    items.filter(
      (item) => item.kind === "action" || Boolean(item.recommended_action)
    ).length
  );
  const meetingCount = storedCount(
    content?.overview?.meetings,
    meetings.filter((item) => item.status !== "cancelled").length
  );
  return {
    items,
    meetings,
    deadlines,
    importantUpdates,
    pendingActions,
    meetingCount,
    notes: content?.notes ?? "",
    structured: Boolean(content?.items?.length || content?.meetings || content?.upcoming_deadlines),
  };
}

export function formatBriefingDate(iso?: string | null): string {
  if (!iso) return "";
  const parsed = new Date(iso);
  if (Number.isNaN(parsed.getTime())) {
    if (/^\d{4}-\d{2}-\d{2}$/.test(iso)) {
      return new Date(`${iso}T12:00:00`).toLocaleDateString(undefined, {
        month: "short",
        day: "numeric",
        year: "numeric",
      });
    }
    return iso;
  }
  return parsed.toLocaleDateString(undefined, {
    timeZone: "Asia/Kolkata",
    month: "short",
    day: "numeric",
    year: "numeric",
  });
}

export function senderName(raw?: string | null): string {
  if (!raw) return "";
  return raw.replace(/<[^>]+>/, "").replace(/^"|"$/g, "").trim() || raw;
}
