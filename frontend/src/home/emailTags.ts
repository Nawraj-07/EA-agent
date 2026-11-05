export type TopicTag =
  | "ICAI"
  | "Taxmann"
  | "Payments"
  | "Deadlines"
  | "Events"
  | "Meetings";

export type PriorityTag = "low" | "medium" | "high";

export type GlanceAnalysis = {
  importance?: string | null;
  deadlines?: string[] | null;
};

export type GlanceEmail = {
  sender?: string | null;
  subject?: string | null;
  snippet?: string | null;
  body?: string | null;
  email_analyses?: GlanceAnalysis | GlanceAnalysis[] | null;
};

const MAX_TOPIC_TABS = 2;

function firstAnalysis(
  analyses: GlanceEmail["email_analyses"]
): GlanceAnalysis | null {
  if (!analyses) return null;
  if (Array.isArray(analyses)) return analyses[0] ?? null;
  return analyses;
}

function haystack(email: GlanceEmail): string {
  return [email.sender, email.subject, email.snippet, email.body]
    .filter(Boolean)
    .join("\n")
    .toLowerCase();
}

export function deriveTopicTags(email: GlanceEmail): TopicTag[] {
  const text = haystack(email);
  const analysis = firstAnalysis(email.email_analyses);
  const tags: TopicTag[] = [];

  if (text.includes("icai")) tags.push("ICAI");
  if (/taxmann|\btaxman\b/.test(text)) tags.push("Taxmann");
  if (
    /\b(payments?|invoices?|paid|payouts?|receipts?)\b/.test(text)
  ) {
    tags.push("Payments");
  }

  const hasStoredDeadlines =
    Array.isArray(analysis?.deadlines) && analysis.deadlines.length > 0;
  if (hasStoredDeadlines || /deadline|due date|last date/.test(text)) {
    tags.push("Deadlines");
  }

  if (/\b(events?|webinars?|conferences?|workshops?|seminars?)\b/.test(text)) {
    tags.push("Events");
  }
  if (/\b(meetings?|invite|zoom|teams|calendar)\b/.test(text)) {
    tags.push("Meetings");
  }

  return tags.slice(0, MAX_TOPIC_TABS);
}

export function derivePriorityTag(email: GlanceEmail): PriorityTag | null {
  const raw = (firstAnalysis(email.email_analyses)?.importance || "")
    .trim()
    .toLowerCase();
  if (raw === "low" || raw === "medium" || raw === "high") return raw;
  return null;
}
