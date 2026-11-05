import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Link } from "react-router-dom";
import ChatPanel from "../ChatPanel";
import { useBriefingHistory } from "../hooks/useBriefingHistory";
import { useBriefingMode } from "../hooks/useBriefingMode";
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
import EmailGlanceTabs from "./EmailGlanceTabs";
import DailyBriefingBody from "./DailyBriefingBody";
import CatchupPanel from "./CatchupPanel";
import EndMyDayPanel from "./EndMyDayPanel";
import { MODE_FILTERS } from "./briefingModeUtils";
import { API } from "../api";
const AUTO_REFRESH_MS = 60_000;
const PAGE_SIZE = 20;
const autoBriefingAttempted = new Set<string>();

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
  "Show my daily briefing",
  "What important emails need my attention?",
  "Which emails require a reply today?",
];

const SELECTED_EMAIL_SUGGESTIONS = [
  "Summarize this email",
  "Draft a reply to this email",
  "What action do I need to take?",
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
    return "";
  }
  const parsed = new Date(`${isoDate}T12:00:00`);
  if (Number.isNaN(parsed.getTime())) {
    return "";
  }
  // Full calendar date, e.g. "November 4, 2026"
  return parsed.toLocaleDateString("en-US", {
    month: "long",
    day: "numeric",
    year: "numeric",
  });
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

  const {
    mode: briefingMode,
    setMode: setBriefingMode,
    mondayVisible,
    modeContent,
    modeLoading,
    modeError,
    modeHasBriefing,
    regenerateMode,
    reloadMode,
  } = useBriefingMode(history?.timezone);

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
  const [detailInitialTab, setDetailInitialTab] = useState<
    "summary" | "thread" | "draft" | undefined
  >(undefined);
  const [detailSeedDraft, setDetailSeedDraft] = useState<{
    id: string;
    email_id: string;
    body: string;
    status: string;
  } | null>(null);
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
  const [catchupEnabled, setCatchupEnabled] = useState(false);
  const [catchupOpen, setCatchupOpen] = useState(false);
  const [endMyDayEnabled, setEndMyDayEnabled] = useState(false);
  const [endMyDayOpen, setEndMyDayOpen] = useState(false);
  const autoBriefingAttemptedRef = useRef(autoBriefingAttempted);
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
    let cancelled = false;
    void (async () => {
      try {
        const response = await fetch(`${API}/db/catchup/status`);
        if (!response.ok) return;
        const data = await response.json();
        if (!cancelled) setCatchupEnabled(Boolean(data?.enabled));
      } catch {
        if (!cancelled) setCatchupEnabled(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    let cancelled = false;
    void (async () => {
      try {
        const response = await fetch(`${API}/db/end-my-day/status`);
        if (!response.ok) return;
        const data = await response.json();
        if (!cancelled) setEndMyDayEnabled(Boolean(data?.enabled));
      } catch {
        if (!cancelled) setEndMyDayEnabled(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    const timer = window.setInterval(() => {
      void loadConversations({ silent: true });
    }, AUTO_REFRESH_MS);
    return () => window.clearInterval(timer);
  }, [loadConversations]);

  const generateBriefing = useCallback(async () => {
    if (generatingBriefing) return;

    setGeneratingBriefing(true);
    setBriefingActionError("");
    setError("");

    try {
      if (briefingMode !== "morning") {
        await regenerateMode();
        return;
      }

      const dateForGenerate = selectedDate || history?.today;
      if (!dateForGenerate) {
        setBriefingActionError("Select a date before generating a briefing.");
        return;
      }

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
  }, [
    generatingBriefing,
    briefingMode,
    regenerateMode,
    selectedDate,
    history?.today,
    reloadBriefing,
  ]);

  useEffect(() => {
    // Morning empty-day auto-generate only — never fire OpenAI on mode filter switch.
    if (briefingMode !== "morning") return;
    if (briefingLoading || generatingBriefing) return;
    if (!selectedDate || !selectedDay) return;
    if (selectedDay.has_briefing) return;
    if (autoBriefingAttemptedRef.current.has(selectedDate)) return;

    autoBriefingAttemptedRef.current.add(selectedDate);
    void generateBriefing();
  }, [
    briefingMode,
    briefingLoading,
    generatingBriefing,
    selectedDate,
    selectedDay,
    generateBriefing,
  ]);

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
      if (briefingMode !== "morning") {
        await reloadMode();
      }
      await loadConversations({ silent: true });
    } catch (err) {
      setError(err instanceof Error ? err.message : "Sync failed");
    } finally {
      setRefreshing(false);
    }
  }

  useEffect(() => {
    const params = new URLSearchParams(window.location.search);
    if (params.get("gmail") !== "connected") return;
    window.history.replaceState({}, "", window.location.pathname);
    void refreshInbox();
    // One controlled sync after OAuth callback. Refresh inbox is stable enough for mount.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  async function selectConversation(conv: Conversation) {
    setDetailInitialTab(undefined);
    setDetailSeedDraft(null);
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

  async function openEmailById(
    emailId: string,
    opts?: {
      tab?: "summary" | "thread" | "draft";
      draft?: {
        id: string;
        email_id: string;
        body: string;
        status: string;
      } | null;
    }
  ) {
    setDetailInitialTab(opts?.tab);
    setDetailSeedDraft(
      opts?.draft && opts.draft.email_id === emailId ? opts.draft : null
    );
    setDetailOpen(true);
    setDetailLoading(true);
    setDetailError("");
    try {
      const response = await fetch(`${API}/db/emails/${emailId}`);
      if (!response.ok) {
        throw new Error(
          response.status === 404
            ? "This email is no longer available in Buddy AI."
            : `Failed to load email (${response.status})`
        );
      }
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
    setDetailInitialTab(undefined);
    setDetailSeedDraft(null);
  }, []);

  const [analysisRequested, setAnalysisRequested] = useState(false);
  const [analysisError, setAnalysisError] = useState("");

  useEffect(() => {
    setAnalysisRequested(false);
    setAnalysisError("");
  }, [selectedEmail?.id]);

  const analysis = getAnalysis(selectedEmail);
  const analysisPending =
    Boolean(selectedEmail) && !analysis && analysisRequested;
  const analysisFailed =
    Boolean(selectedEmail) &&
    !analysis &&
    selectedEmail?.analysis_status === "failed" &&
    !analysisRequested;

  useEffect(() => {
    if (!selectedEmail?.id || analysis) {
      return;
    }
    if (!analysisRequested && selectedEmail.analysis_status !== "pending") {
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
  }, [selectedEmail?.id, selectedEmail?.analysis_status, analysisRequested, analysis]);

  async function requestEmailAnalysis() {
    if (!selectedEmail?.id || analysisRequested) return;
    setAnalysisRequested(true);
    setAnalysisError("");
    try {
      const response = await fetch(
        `${API}/db/emails/${selectedEmail.id}/analyze`,
        { method: "POST" }
      );
      if (!response.ok) {
        throw new Error(`Analysis failed (${response.status})`);
      }
      const payload = await response.json();
      if (payload?.email) {
        setSelectedEmail(payload.email);
      }
    } catch (err) {
      setAnalysisRequested(false);
      setAnalysisError(
        err instanceof Error ? err.message : "Unable to generate AI summary"
      );
    }
  }

  const morningContent = selectedDay?.briefing?.content;
  const briefingContent =
    briefingMode === "morning" ? morningContent : modeContent;
  const showBriefingLoading =
    briefingMode === "morning"
      ? briefingLoading && !briefingContent
      : modeLoading && !briefingContent;
  const hasDisplayBriefing =
    briefingMode === "morning"
      ? Boolean(selectedDay?.has_briefing && briefingContent)
      : Boolean(modeHasBriefing && briefingContent);
  const modeTitle =
    briefingMode === "morning"
      ? "Daily Briefing"
      : briefingMode === "evening"
        ? "Evening Wrap-up"
        : briefingMode === "weekly"
          ? "Weekly Digest"
          : "Monday Planning";

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
            {catchupEnabled ? (
              <button
                type="button"
                className="home-catchup-btn"
                onClick={() => {
                  setEndMyDayOpen(false);
                  setCatchupOpen(true);
                }}
              >
                What Did I Miss?
              </button>
            ) : null}
            {endMyDayEnabled ? (
              <button
                type="button"
                className="home-catchup-btn"
                onClick={() => {
                  setCatchupOpen(false);
                  setEndMyDayOpen(true);
                }}
              >
                End My Day
              </button>
            ) : null}
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
            <ProfileChip date={dateParts.date} timezone={dateParts.timezone} />
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
                        <div className="home-conv-top-end">
                          <EmailGlanceTabs email={conv} />
                          <time>
                            {formatTime(conv.internal_date, conv.received_at)}
                          </time>
                        </div>
                      </div>
                      <div className="home-conv-sender">
                        {conv.sender || "Unknown sender"}
                      </div>
                      <div className="home-conv-snippet">
                        {conv.snippet || "No preview"}
                      </div>
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

        <section
          className={`home-briefing-card home-briefing-col${
            catchupOpen || endMyDayOpen ? " catchup-active" : ""
          }`}
          aria-label={
            endMyDayOpen
              ? "End My Day"
              : catchupOpen
                ? "What Did I Miss"
                : "Daily Briefing"
          }
        >
          {endMyDayEnabled && endMyDayOpen ? (
            <EndMyDayPanel
              open={endMyDayOpen}
              onClose={() => setEndMyDayOpen(false)}
              onOpenEmail={(id, opts) => void openEmailById(id, opts)}
            />
          ) : catchupEnabled && catchupOpen ? (
            <CatchupPanel
              open={catchupOpen}
              onClose={() => setCatchupOpen(false)}
              onOpenEmail={(id, opts) => void openEmailById(id, opts)}
            />
          ) : (
            <>
              <div className="home-briefing-card-head">
                <div className="home-briefing-title">
                  <div className="home-briefing-icon" aria-hidden>
                    ✓
                  </div>
                  <div>
                    <h2>{modeTitle}</h2>
                    <p>
                      {briefingMode === "morning"
                        ? briefingLoading && !selectedDate
                          ? "Loading…"
                          : formatBriefingSubtitle(
                              selectedDate || history?.today || ""
                            )
                        : briefingMode === "evening"
                          ? "Today's evening wrap-up"
                          : briefingMode === "weekly"
                            ? "Latest weekly digest"
                            : "This week's Monday plan"}
                    </p>
                  </div>
                </div>
                <div className="home-briefing-actions">
                  <button
                    type="button"
                    className="home-briefing-btn"
                    onClick={() => void generateBriefing()}
                    disabled={
                      generatingBriefing ||
                      (briefingMode === "morning"
                        ? briefingLoading || !selectedDate
                        : modeLoading)
                    }
                  >
                    {generatingBriefing
                      ? "Generating…"
                      : hasDisplayBriefing
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
              <div
                className="home-briefing-mode-filters"
                role="tablist"
                aria-label="Briefing mode"
              >
                {MODE_FILTERS.filter(
                  (f) => f.id !== "monday" || mondayVisible
                ).map((filter) => {
                  const active = briefingMode === filter.id;
                  return (
                    <button
                      key={filter.id}
                      type="button"
                      role="tab"
                      aria-selected={active}
                      className={
                        active
                          ? "home-briefing-mode-chip active"
                          : "home-briefing-mode-chip"
                      }
                      onClick={() => setBriefingMode(filter.id)}
                    >
                      {filter.label}
                    </button>
                  );
                })}
              </div>
              {showBriefingLoading ? (
                <p className="home-empty" style={{ padding: "8px 0" }}>
                  Loading briefing…
                </p>
              ) : !hasDisplayBriefing ? (
                <p className="home-empty" style={{ padding: "8px 0" }}>
                  {generatingBriefing
                    ? briefingMode === "morning"
                      ? "Generating today's briefing…"
                      : `Generating ${modeTitle.toLowerCase()}…`
                    : briefingActionError || modeError
                      ? "Briefing could not be loaded. Use Generate to try again."
                      : briefingMode === "morning"
                        ? "No briefing for this day yet."
                        : `${modeTitle} is not ready yet. It will appear after the scheduled run.`}
                </p>
              ) : (
                <DailyBriefingBody
                  content={briefingContent ?? null}
                  showTodayOverview={
                    briefingMode === "morning" &&
                    selectedDate === history?.today
                  }
                  onOpenEmail={(id, opts) => void openEmailById(id, opts)}
                />
              )}
            </>
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
          expandable
          suggestions={
            selectedEmail ? SELECTED_EMAIL_SUGGESTIONS : SUGGESTIONS
          }
          emailId={selectedEmail?.id}
          onOpenEmail={(id, opts) => void openEmailById(id, opts)}
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
        analysisError={analysisError}
        onRequestAnalysis={() => void requestEmailAnalysis()}
        formatTime={formatTime}
        initialTab={detailInitialTab}
        initialDraft={detailSeedDraft}
      />
    </div>
  );
}
