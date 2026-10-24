import { useCallback, useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";
import ChatPanel from "../ChatPanel";
import { useBriefingHistory } from "../hooks/useBriefingHistory";
import "../Inbox.css";
import "./Home.css";
import EmailDetailPopup from "./EmailDetailPopup";
import {
  inboxCacheKey,
  readInboxCache,
  readLastInboxCache,
  writeInboxCache,
} from "./inboxCache";
import ProfileChip from "./ProfileChip";
import { API } from "../api";
const AUTO_REFRESH_MS = 60_000;
const PAGE_SIZE = 20;

type InboxCategory =
  | "inbox"
  | "primary"
  | "updates"
  | "promotions"
  | "social"
  | "forums";

type EmailAnalysis = {
  summary: string;
  category: string;
  importance: string;
  deadlines: string[];
  action_required: boolean;
  confidence: number;
};

type Conversation = {
  thread_id: string;
  latest_email_id: string;
  subject: string | null;
  sender: string | null;
  snippet: string | null;
  internal_date: number | null;
  received_at: string | null;
  analysis_status?: string;
  email_analyses?: EmailAnalysis | null;
  message_count?: number;
};

type Email = {
  id: string;
  gmail_thread_id?: string | null;
  subject: string | null;
  sender: string | null;
  received_at: string | null;
  snippet: string | null;
  body?: string | null;
  internal_date?: number | null;
  analysis_status?: string;
  email_analyses?: EmailAnalysis[] | EmailAnalysis | null;
};

type ConversationsResponse = {
  conversations: Conversation[];
  next_cursor: { internal_date: number; thread_id: string } | null;
};

const CATEGORY_TABS: { id: InboxCategory; label: string }[] = [
  { id: "inbox", label: "All Inbox" },
  { id: "primary", label: "Primary" },
  { id: "updates", label: "Updates" },
  { id: "promotions", label: "Promotions" },
  { id: "social", label: "Social" },
];

const SUGGESTIONS = [
  "What important emails need my attention?",
  "Which emails require a reply today?",
  "Summarize high-importance messages this week",
];

function greetingForHour(hour: number): string {
  if (hour < 12) return "Good morning";
  if (hour < 17) return "Good afternoon";
  return "Good evening";
}

function getAnalysis(email: Email | null): EmailAnalysis | null {
  if (!email) return null;
  const analyses = email.email_analyses;
  if (Array.isArray(analyses)) return analyses[0] ?? null;
  return analyses ?? null;
}

function formatTime(internalDate: number | null | undefined, fallback: string | null) {
  if (internalDate) {
    return new Date(internalDate).toLocaleString(undefined, {
      hour: "numeric",
      minute: "2-digit",
    });
  }
  if (fallback) return new Date(fallback).toLocaleString();
  return "";
}

function formatDayLabel(isoDate: string): { weekday: string; day: string } {
  const parsed = new Date(`${isoDate}T12:00:00`);
  return {
    weekday: parsed.toLocaleDateString(undefined, { weekday: "short" }),
    day: parsed.toLocaleDateString(undefined, { day: "numeric" }),
  };
}

function extractDetail(data: unknown): string | null {
  if (!data || typeof data !== "object") return null;
  const detail = (data as { detail?: unknown }).detail;
  return typeof detail === "string" ? detail : null;
}

function senderInitial(sender: string | null | undefined): string {
  if (!sender) return "?";
  const name = sender.replace(/<.*>/, "").trim();
  const ch = name.charAt(0);
  return ch ? ch.toUpperCase() : "?";
}

function formatBriefingSubtitle(isoDate: string, headline?: string): string {
  if (headline) return headline;
  if (!isoDate || !/^\d{4}-\d{2}-\d{2}$/.test(isoDate)) {
    return "Daily Executive Briefing";
  }
  const parsed = new Date(`${isoDate}T12:00:00`);
  if (Number.isNaN(parsed.getTime())) {
    return "Daily Executive Briefing";
  }
  return `Daily Executive Briefing – ${parsed.toLocaleDateString(undefined, {
    month: "long",
    day: "numeric",
    year: "numeric",
  })}`;
}

export default function HomePage() {
  const {
    history,
    selectedDate,
    setSelectedDate,
    selectedDay,
    visibleRange,
    rangeReady,
    loading: briefingLoading,
    error: briefingError,
    reload: reloadBriefing,
  } = useBriefingHistory();

  const [category, setCategory] = useState<InboxCategory>(() => {
    const last = readLastInboxCache<Conversation>()?.key.category;
    return CATEGORY_TABS.some((tab) => tab.id === last)
      ? (last as InboxCategory)
      : "inbox";
  });
  const [conversations, setConversations] = useState<Conversation[]>(
    () => readLastInboxCache<Conversation>()?.conversations ?? []
  );
  const [nextCursor, setNextCursor] = useState<
    ConversationsResponse["next_cursor"]
  >(() => readLastInboxCache<Conversation>()?.nextCursor ?? null);
  const [selectedEmail, setSelectedEmail] = useState<Email | null>(null);
  const [threadMessages, setThreadMessages] = useState<Email[]>([]);
  const [detailOpen, setDetailOpen] = useState(false);
  const [loading, setLoading] = useState(
    () => !(readLastInboxCache<Conversation>()?.conversations.length)
  );
  const [refreshing, setRefreshing] = useState(false);
  const [loadingMore, setLoadingMore] = useState(false);
  const [detailLoading, setDetailLoading] = useState(false);
  const [error, setError] = useState("");
  const [detailError, setDetailError] = useState("");
  const [generatingBriefing, setGeneratingBriefing] = useState(false);
  const [briefingActionError, setBriefingActionError] = useState("");
  const dateFrom = visibleRange?.startDate ?? null;
  const dateTo = visibleRange?.endDate ?? null;

  const greeting = useMemo(
    () => greetingForHour(new Date().getHours()),
    []
  );

  const dateParts = useMemo(() => {
    const now = new Date();
    const date = now.toLocaleDateString(undefined, {
      weekday: "short",
      day: "numeric",
      month: "short",
      year: "numeric",
    });
    return {
      date,
      timezone: history?.timezone || "Asia/Kolkata",
    };
  }, [history?.timezone]);

  const loadConversations = useCallback(
    async (options?: {
      silent?: boolean;
      append?: boolean;
      cursor?: ConversationsResponse["next_cursor"];
      cat?: InboxCategory;
      from?: string | null;
      to?: string | null;
    }) => {
      const append = options?.append ?? false;
      const cat = options?.cat ?? category;
      const from = options?.from !== undefined ? options.from : dateFrom;
      const to = options?.to !== undefined ? options.to : dateTo;
      const key = inboxCacheKey(cat, from, to);
      const cached = readInboxCache<Conversation>(key);
      const silent =
        (options?.silent ?? false) ||
        Boolean(cached?.conversations.length);

      try {
        if (append) setLoadingMore(true);
        else if (silent) setRefreshing(true);
        else setLoading(true);
        setError("");

        const params = new URLSearchParams({
          category: cat,
          limit: String(PAGE_SIZE),
        });
        if (options?.cursor) {
          params.set("cursor_internal_date", String(options.cursor.internal_date));
          params.set("cursor_thread_id", options.cursor.thread_id);
        }
        if (from) params.set("date_from", from);
        if (to) params.set("date_to", to);

        const response = await fetch(
          `${API}/db/conversations?${params.toString()}`
        );
        if (!response.ok) {
          const body = await response.json().catch(() => null);
          const detail =
            body && typeof body === "object" && "detail" in body
              ? String((body as { detail: unknown }).detail)
              : null;
          throw new Error(
            detail || `Failed to load conversations (${response.status})`
          );
        }
        const data = (await response.json()) as ConversationsResponse;
        if (!Array.isArray(data.conversations)) {
          throw new Error("Unexpected API response");
        }
        const nextCursorValue = data.next_cursor ?? null;
        setConversations((prev) => {
          const next = append
            ? [...prev, ...data.conversations]
            : data.conversations;
          writeInboxCache({
            key,
            conversations: next,
            nextCursor: nextCursorValue,
          });
          return next;
        });
        setNextCursor(nextCursorValue);
      } catch (err) {
        setError(err instanceof Error ? err.message : "Something went wrong");
      } finally {
        setLoading(false);
        setRefreshing(false);
        setLoadingMore(false);
      }
    },
    [category, dateFrom, dateTo]
  );

  useEffect(() => {
    if (!rangeReady) return;
    const key = inboxCacheKey(category, dateFrom, dateTo);
    const cached = readInboxCache<Conversation>(key);
    if (cached?.conversations.length) {
      setConversations(cached.conversations);
      setNextCursor(cached.nextCursor);
      setLoading(false);
    } else {
      setConversations([]);
      setNextCursor(null);
      setLoading(true);
    }
    void loadConversations({
      cat: category,
      from: dateFrom,
      to: dateTo,
      silent: Boolean(cached?.conversations.length),
    });
  }, [category, rangeReady, dateFrom, dateTo, loadConversations]);

  useEffect(() => {
    const timer = window.setInterval(() => {
      void loadConversations({ silent: true });
    }, AUTO_REFRESH_MS);
    return () => window.clearInterval(timer);
  }, [loadConversations]);

  async function generateBriefing() {
    if (generatingBriefing) return;

    const dateForGenerate = selectedDate || history?.today;
    if (!dateForGenerate) {
      setBriefingActionError("Select a date before generating a briefing.");
      return;
    }

    setGeneratingBriefing(true);
    setBriefingActionError("");
    setError("");

    try {
      const params = new URLSearchParams({ date: dateForGenerate });
      const response = await fetch(
        `${API}/internal/briefing-now?${params.toString()}`,
        { method: "POST" }
      );
      const data = await response.json().catch(() => null);

      if (!response.ok) {
        throw new Error(
          extractDetail(data) ||
            `Briefing generation failed (${response.status})`
        );
      }

      await reloadBriefing(dateForGenerate);
    } catch (err) {
      setBriefingActionError(
        err instanceof Error ? err.message : "Briefing generation failed"
      );
    } finally {
      setGeneratingBriefing(false);
    }
  }

  async function refreshInbox() {
    setRefreshing(true);
    setError("");
    try {
      const syncResponse = await fetch(`${API}/db/inbox/sync`, {
        method: "POST",
      });
      if (!syncResponse.ok) {
        const body = await syncResponse.json().catch(() => null);
        const detail =
          body && typeof body === "object" && "detail" in body
            ? String((body as { detail: unknown }).detail)
            : null;
        throw new Error(detail || `Inbox sync failed (${syncResponse.status})`);
      }
      await reloadBriefing(selectedDate || undefined);
      await loadConversations({ silent: true });
    } catch (err) {
      setError(err instanceof Error ? err.message : "Sync failed");
      setRefreshing(false);
    }
  }

  async function selectConversation(conv: Conversation) {
    setDetailOpen(true);
    setDetailLoading(true);
    setDetailError("");
    setSelectedEmail(null);
    setThreadMessages([]);
    try {
      const threadResponse = await fetch(
        `${API}/db/conversations/${encodeURIComponent(conv.thread_id)}`
      );
      if (threadResponse.ok) {
        const threadData = (await threadResponse.json()) as {
          messages: Email[];
        };
        const messages = Array.isArray(threadData.messages)
          ? threadData.messages
          : [];
        setThreadMessages(messages);
        const latest =
          messages.find((m) => m.id === conv.latest_email_id) ||
          messages[messages.length - 1] ||
          null;
        if (latest) {
          setSelectedEmail(latest);
          setDetailLoading(false);
          return;
        }
      }
      const response = await fetch(`${API}/db/emails/${conv.latest_email_id}`);
      if (!response.ok) {
        throw new Error(`Failed to load email details (${response.status})`);
      }
      const data: Email = await response.json();
      setSelectedEmail(data);
      setThreadMessages([data]);
    } catch (err) {
      setDetailError(err instanceof Error ? err.message : "Something went wrong");
    } finally {
      setDetailLoading(false);
    }
  }

  async function openEmailById(emailId: string) {
    setDetailOpen(true);
    setDetailLoading(true);
    setDetailError("");
    try {
      const response = await fetch(`${API}/db/emails/${emailId}`);
      if (!response.ok) throw new Error(`Failed to load email (${response.status})`);
      const data: Email = await response.json();
      setSelectedEmail(data);
      setThreadMessages((prev) =>
        prev.some((m) => m.id === data.id) ? prev : [...prev, data]
      );
    } catch (err) {
      setDetailError(err instanceof Error ? err.message : "Something went wrong");
    } finally {
      setDetailLoading(false);
    }
  }

  const closeDetail = useCallback(() => {
    setDetailOpen(false);
  }, []);

  const analysis = getAnalysis(selectedEmail);
  const analysisPending =
    Boolean(selectedEmail) &&
    !analysis &&
    selectedEmail?.analysis_status !== "failed";
  const analysisFailed =
    Boolean(selectedEmail) &&
    !analysis &&
    selectedEmail?.analysis_status === "failed";

  useEffect(() => {
    if (!selectedEmail?.id || analysis || selectedEmail.analysis_status === "failed") {
      return;
    }
    let cancelled = false;
    const timer = window.setInterval(async () => {
      try {
        const response = await fetch(`${API}/db/emails/${selectedEmail.id}`);
        if (!response.ok || cancelled) return;
        const data: Email = await response.json();
        if (cancelled) return;
        setSelectedEmail(data);
      } catch {
        /* Keep showing Analyzing until the next poll. */
      }
    }, 4000);
    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
  }, [selectedEmail?.id, selectedEmail?.analysis_status, analysis]);

  const briefingContent = selectedDay?.briefing?.content;
  const briefingItems = useMemo(() => {
    const items: { text: string; kind: "action" | "deadline" | "follow" }[] =
      [];
    for (const t of briefingContent?.action_items ?? []) {
      const kind = /follow|reply|respond/i.test(t) ? "follow" : "action";
      items.push({ text: t, kind });
    }
    for (const t of briefingContent?.deadlines ?? []) {
      items.push({ text: t, kind: "deadline" });
    }
    if (!items.length) {
      for (const t of briefingContent?.highlights ?? []) {
        items.push({ text: t, kind: "action" });
      }
    }
    return items;
  }, [briefingContent]);

  return (
    <div className="home-page">
      <header className="home-hero">
        <div className="home-hero-left">
          <div className="home-brand">
            <div className="home-brand-lockup">
              <img
                className="home-brand-icon"
                src="/buddy-logo.jpg"
                alt=""
                width={44}
                height={44}
              />
              <div className="home-brand-text">
                <h1 className="home-logo" aria-label="büddy">
                  <span className="home-logo-dark">b</span>
                  <span className="home-logo-accent">ü</span>
                  <span className="home-logo-dark">ddy</span>
                </h1>
                <span className="home-brand-tagline">
                  Your AI Executive Assistant
                </span>
              </div>
            </div>
          </div>

          <div className="home-greeting">
            <h2>{greeting}</h2>
            <p>Here&apos;s what needs your attention today.</p>
          </div>

          <div className="home-date-strip" role="tablist" aria-label="Briefing dates">
            {(history?.days ?? []).map((day) => {
              const label = formatDayLabel(day.date);
              const active = day.date === selectedDate;
              return (
                <button
                  key={day.date}
                  type="button"
                  role="tab"
                  aria-selected={active}
                  className={`home-date-chip${active ? " active" : ""}`}
                  onClick={() => setSelectedDate(day.date)}
                >
                  <span className="wd">{label.weekday}</span>
                  <span className="dy">{label.day}</span>
                </button>
              );
            })}
          </div>
        </div>

        <div className="home-hero-right">
          <div className="home-hero-visual" aria-hidden>
            <img
              src="/hero-workspace.jpg"
              alt=""
              width={960}
              height={540}
            />
          </div>
          <div className="home-hero-actions">
            <div className="home-date-pill">
              <svg
                className="home-date-icon"
                width="18"
                height="18"
                viewBox="0 0 24 24"
                fill="none"
                aria-hidden
              >
                <rect
                  x="3"
                  y="5"
                  width="18"
                  height="16"
                  rx="2"
                  stroke="currentColor"
                  strokeWidth="1.75"
                />
                <path
                  d="M3 10h18M8 3v4M16 3v4"
                  stroke="currentColor"
                  strokeWidth="1.75"
                  strokeLinecap="round"
                />
              </svg>
              <div className="home-date-text">
                <strong>{dateParts.date}</strong>
                <span>{dateParts.timezone}</span>
              </div>
            </div>
            <button
              type="button"
              className="home-refresh-btn"
              onClick={() => void refreshInbox()}
              disabled={loading || refreshing}
            >
              <svg
                className="home-refresh-icon"
                width="16"
                height="16"
                viewBox="0 0 24 24"
                fill="none"
                aria-hidden
              >
                <path
                  d="M20 12a8 8 0 1 1-2.34-5.66"
                  stroke="currentColor"
                  strokeWidth="2"
                  strokeLinecap="round"
                />
                <path
                  d="M20 4v5h-5"
                  stroke="currentColor"
                  strokeWidth="2"
                  strokeLinecap="round"
                  strokeLinejoin="round"
                />
              </svg>
              {refreshing ? "Syncing…" : "Refresh inbox"}
            </button>
            <ProfileChip />
          </div>
        </div>
      </header>

      {(error || briefingError || briefingActionError) && (
        <div className="home-error" role="alert">
          {error || briefingError || briefingActionError}{" "}
          {/gmail|oauth|reconnect/i.test(
            error || briefingError || briefingActionError
          ) ? (
            <a href={`${API}/auth/google`}>Connect Gmail</a>
          ) : null}
        </div>
      )}

      <div className="home-workspace">
        <section className="home-col" aria-label="Inbox">
          <div className="home-col-head">
            <div>
              <h2>Inbox</h2>
              <p>
                {conversations.length} conversations in selected period
                {visibleRange
                  ? ` (${visibleRange.startDate} → ${visibleRange.endDate}).`
                  : "."}{" "}
                Tap a mail for AI summary.
              </p>
            </div>
          </div>

          <div className="home-tabs" role="tablist">
            {CATEGORY_TABS.map((tab) => (
              <button
                key={tab.id}
                type="button"
                role="tab"
                className={`home-tab${category === tab.id ? " active" : ""}`}
                aria-selected={category === tab.id}
                onClick={() => {
                  setCategory(tab.id);
                  setSelectedEmail(null);
                  setThreadMessages([]);
                  setDetailOpen(false);
                }}
              >
                {tab.label}
              </button>
            ))}
          </div>

          {conversations.length > 0 ? (
            <>
              {conversations.map((conv) => {
                const imp = (
                  conv.email_analyses?.importance || ""
                ).toLowerCase();
                return (
                  <button
                    key={conv.thread_id}
                    type="button"
                    className={`home-conv${
                      selectedEmail?.id === conv.latest_email_id ||
                      selectedEmail?.gmail_thread_id === conv.thread_id
                        ? " active"
                        : ""
                    }`}
                    onClick={() => void selectConversation(conv)}
                  >
                    <span className="home-avatar" aria-hidden>
                      {senderInitial(conv.sender)}
                    </span>
                    <div className="home-conv-body">
                      <div className="home-conv-top">
                        <strong>{conv.subject || "No Subject"}</strong>
                        <time>
                          {formatTime(conv.internal_date, conv.received_at)}
                        </time>
                      </div>
                      <div className="home-conv-sender">
                        {conv.sender || "Unknown sender"}
                      </div>
                      <div className="home-conv-snippet">
                        {conv.snippet || "No preview"}
                      </div>
                      {imp && (
                        <span className={`home-badge ${imp}`}>{imp}</span>
                      )}
                    </div>
                  </button>
                );
              })}
              {nextCursor && (
                <button
                  type="button"
                  className="home-load-more"
                  disabled={loadingMore}
                  onClick={() =>
                    void loadConversations({
                      append: true,
                      cursor: nextCursor,
                    })
                  }
                >
                  {loadingMore ? "Loading…" : "Load more"}
                </button>
              )}
            </>
          ) : !rangeReady || loading ? (
            <p className="home-empty">Loading conversations…</p>
          ) : (
            <p className="home-empty">
              No conversations in this date range. Refresh inbox to sync more
              mail if local data is incomplete.
            </p>
          )}
        </section>

        <section className="home-briefing-card home-briefing-col" aria-label="Daily Briefing">
          <div className="home-briefing-card-head">
            <div className="home-briefing-title">
              <div className="home-briefing-icon" aria-hidden>
                ✓
              </div>
              <div>
                <h2>Daily Briefing</h2>
                <p>
                  {briefingLoading && !selectedDate
                    ? "Loading…"
                    : formatBriefingSubtitle(
                        selectedDate || history?.today || "",
                        briefingContent?.headline
                      )}
                </p>
              </div>
            </div>
            <div className="home-briefing-actions">
              <button
                type="button"
                className="home-briefing-btn"
                onClick={() => void generateBriefing()}
                disabled={
                  briefingLoading || generatingBriefing || !selectedDate
                }
              >
                {generatingBriefing
                  ? "Generating…"
                  : selectedDay?.has_briefing
                    ? "Regenerate"
                    : "Generate"}
              </button>
              <Link
                className="home-view-all"
                to="/briefings"
                aria-label="View all briefings"
                title="View all briefings"
              >
                <svg
                  width="16"
                  height="16"
                  viewBox="0 0 24 24"
                  fill="none"
                  aria-hidden
                >
                  <path
                    d="M5 12h14M13 6l6 6-6 6"
                    stroke="currentColor"
                    strokeWidth="2"
                    strokeLinecap="round"
                    strokeLinejoin="round"
                  />
                </svg>
              </Link>
            </div>
          </div>
          {briefingLoading && briefingItems.length === 0 ? (
            <p className="home-empty" style={{ padding: "8px 0" }}>
              Loading briefing…
            </p>
          ) : briefingItems.length === 0 ? (
            <p className="home-empty" style={{ padding: "8px 0" }}>
              No briefing for this day yet. Use Generate, or wait for the
              scheduled job.
            </p>
          ) : (
            <ul className="home-briefing-items">
              {briefingItems.map((item) => (
                <li key={`${item.kind}-${item.text}`} className="home-briefing-item">
                  <span
                    className={`home-briefing-item-icon ${
                      item.kind === "deadline"
                        ? "deadline"
                        : item.kind === "follow"
                          ? "follow"
                          : ""
                    }`}
                    aria-hidden
                  >
                    {item.kind === "deadline"
                      ? "◎"
                      : item.kind === "follow"
                        ? "⏱"
                        : "✓"}
                  </span>
                  <span className="home-briefing-item-text">{item.text}</span>
                </li>
              ))}
            </ul>
          )}
        </section>
      </div>

      <section className="home-buddy" aria-label="AI buddy">
        <div className="home-col-head">
          <div>
            <h2>AI buddy</h2>
            <p>
              {selectedEmail
                ? "Asking about the selected email (follow-ups supported)"
                : "Search across saved mail by sender, date, keywords, or importance"}
            </p>
          </div>
        </div>
        <ChatPanel
          embedded
          suggestions={SUGGESTIONS}
          emailId={selectedEmail?.id}
          onOpenEmail={(id) => void openEmailById(id)}
        />
      </section>

      <EmailDetailPopup
        open={detailOpen}
        onClose={closeDetail}
        selectedEmail={selectedEmail}
        threadMessages={threadMessages}
        onSelectThreadMessage={setSelectedEmail}
        detailLoading={detailLoading}
        detailError={detailError}
        analysis={analysis}
        analysisPending={analysisPending}
        analysisFailed={analysisFailed}
        formatTime={formatTime}
      />
    </div>
  );
}
