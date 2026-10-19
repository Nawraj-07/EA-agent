import { useCallback, useEffect, useState } from "react";
import "./Inbox.css";
import ChatPanel from "./ChatPanel";
import DraftPanel from "./DraftPanel";
import BriefingPanel from "./BriefingPanel";

const API = import.meta.env.VITE_API_URL || "http://localhost:8000";
const AUTO_REFRESH_MS = 60_000;
const PAGE_SIZE = 20;

type InboxCategory = "primary" | "promotions" | "social" | "inbox";

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
  gmail_message_id: string;
  subject: string | null;
  sender: string | null;
  snippet: string | null;
  internal_date: number | null;
  received_at: string | null;
  label_ids?: string[];
  analysis_status?: string;
  email_analyses?: EmailAnalysis | null;
  message_count?: number;
};

type Email = {
  id: string;
  gmail_message_id: string;
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
  category: string;
};

const CATEGORY_TABS: { id: InboxCategory; label: string }[] = [
  { id: "primary", label: "Primary" },
  { id: "promotions", label: "Promotions" },
  { id: "social", label: "Social" },
  { id: "inbox", label: "All Inbox" },
];

function getAnalysis(email: Email | null): EmailAnalysis | null {
  if (!email) return null;
  const analyses = email.email_analyses;
  if (Array.isArray(analyses)) {
    return analyses[0] ?? null;
  }
  return analyses ?? null;
}

function formatActivity(internalDate: number | null | undefined, fallback: string | null) {
  if (internalDate) {
    return new Date(internalDate).toLocaleString();
  }
  return fallback || "Unknown";
}

export default function Inbox() {
  const [category, setCategory] = useState<InboxCategory>("primary");
  const [conversations, setConversations] = useState<Conversation[]>([]);
  const [nextCursor, setNextCursor] = useState<ConversationsResponse["next_cursor"]>(null);
  const [selectedEmail, setSelectedEmail] = useState<Email | null>(null);
  const [threadMessages, setThreadMessages] = useState<Email[]>([]);

  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [loadingMore, setLoadingMore] = useState(false);
  const [detailLoading, setDetailLoading] = useState(false);
  const [error, setError] = useState("");
  const [detailError, setDetailError] = useState("");
  const [lastRefreshedAt, setLastRefreshedAt] = useState<Date | null>(null);

  const loadConversations = useCallback(
    async (options?: {
      silent?: boolean;
      append?: boolean;
      cursor?: ConversationsResponse["next_cursor"];
      cat?: InboxCategory;
    }) => {
      const silent = options?.silent ?? false;
      const append = options?.append ?? false;
      const cat = options?.cat ?? category;

      try {
        if (append) {
          setLoadingMore(true);
        } else if (silent) {
          setRefreshing(true);
        } else {
          setLoading(true);
        }
        setError("");

        const params = new URLSearchParams({
          category: cat,
          limit: String(PAGE_SIZE),
        });
        if (options?.cursor) {
          params.set(
            "cursor_internal_date",
            String(options.cursor.internal_date)
          );
          params.set("cursor_thread_id", options.cursor.thread_id);
        }

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

        setConversations((prev) =>
          append ? [...prev, ...data.conversations] : data.conversations
        );
        setNextCursor(data.next_cursor ?? null);
        setLastRefreshedAt(new Date());
      } catch (err) {
        setError(err instanceof Error ? err.message : "Something went wrong");
      } finally {
        setLoading(false);
        setRefreshing(false);
        setLoadingMore(false);
      }
    },
    [category]
  );

  useEffect(() => {
    void loadConversations({ cat: category });
  }, [category, loadConversations]);

  useEffect(() => {
    const timer = window.setInterval(() => {
      void loadConversations({ silent: true });
    }, AUTO_REFRESH_MS);

    return () => window.clearInterval(timer);
  }, [loadConversations]);

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
        throw new Error(
          detail || `Inbox sync failed (${syncResponse.status})`
        );
      }
      await loadConversations({ silent: true });
    } catch (err) {
      setError(err instanceof Error ? err.message : "Sync failed");
      setRefreshing(false);
    }
  }

  async function selectConversation(conv: Conversation) {
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

      const response = await fetch(
        `${API}/db/emails/${conv.latest_email_id}`
      );
      if (!response.ok) {
        throw new Error(
          `Failed to load email details (${response.status})`
        );
      }
      const data: Email = await response.json();
      setSelectedEmail(data);
      setThreadMessages([data]);
    } catch (err) {
      setDetailError(
        err instanceof Error ? err.message : "Something went wrong"
      );
    } finally {
      setDetailLoading(false);
    }
  }

  const analysis = getAnalysis(selectedEmail);
  const refreshedLabel = lastRefreshedAt
    ? lastRefreshedAt.toLocaleTimeString()
    : null;

  return (
    <div className="inbox-page">
      <header className="inbox-header">
        <div>
          <h1>AI Inbox</h1>
          <p>
            Conversations ordered like Gmail (newest activity first). Default
            tab: Primary.
          </p>
          {refreshedLabel && (
            <p className="inbox-refreshed">
              Last refreshed {refreshedLabel}
              {refreshing ? " · syncing…" : ""}
            </p>
          )}
        </div>

        <div className="inbox-header-actions">
          <button
            type="button"
            className="inbox-refresh-btn"
            onClick={() => void refreshInbox()}
            disabled={loading || refreshing}
          >
            {refreshing ? "Syncing…" : "Refresh inbox"}
          </button>
          <span className="email-count">
            {conversations.length} Conversations
          </span>
        </div>
      </header>

      {error && <div className="inbox-error">{error}</div>}

      <BriefingPanel />

      <div className="inbox-category-tabs" role="tablist">
        {CATEGORY_TABS.map((tab) => (
          <button
            key={tab.id}
            type="button"
            role="tab"
            aria-selected={category === tab.id}
            className={`inbox-category-tab ${
              category === tab.id ? "active" : ""
            }`}
            onClick={() => {
              setCategory(tab.id);
              setSelectedEmail(null);
              setThreadMessages([]);
            }}
          >
            {tab.label}
          </button>
        ))}
      </div>

      {loading ? (
        <p className="inbox-message">Loading conversations…</p>
      ) : error && conversations.length === 0 ? null : conversations.length ===
        0 ? (
        <p className="inbox-message">
          No conversations in this view yet. Click Refresh inbox to sync from
          Gmail (requires ENABLE_MANUAL_SYNC=true), or wait for the 15-minute
          scheduler.
        </p>
      ) : (
        <div className="inbox-layout">
          <aside className="email-list">
            <h2>Conversations</h2>

            {conversations.map((conv) => {
              const itemAnalysis = conv.email_analyses ?? null;

              return (
                <button
                  key={conv.thread_id}
                  type="button"
                  className={`email-item ${
                    selectedEmail?.id === conv.latest_email_id ||
                    selectedEmail?.gmail_thread_id === conv.thread_id
                      ? "active"
                      : ""
                  }`}
                  onClick={() => void selectConversation(conv)}
                >
                  <div className="email-item-top">
                    <strong>{conv.subject || "No Subject"}</strong>
                    <span
                      className={`importance ${(
                        itemAnalysis?.importance || "low"
                      ).toLowerCase()}`}
                    >
                      {itemAnalysis?.importance ||
                        conv.analysis_status ||
                        "N/A"}
                    </span>
                  </div>

                  <p>{conv.sender || "Unknown Sender"}</p>

                  <small>
                    {conv.snippet || "No preview available"}
                    {conv.message_count && conv.message_count > 1
                      ? ` · ${conv.message_count} messages`
                      : ""}
                  </small>
                </button>
              );
            })}

            {nextCursor && (
              <button
                type="button"
                className="inbox-load-more"
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
          </aside>

          <main className="email-detail">
            {detailLoading ? (
              <p>Loading email details...</p>
            ) : detailError ? (
              <p className="inbox-error">{detailError}</p>
            ) : !selectedEmail ? (
              <div className="empty-detail">
                Select a conversation to view its AI analysis.
              </div>
            ) : (
              <>
                <h2>{selectedEmail.subject || "No Subject"}</h2>

                <p>
                  <strong>From:</strong>{" "}
                  {selectedEmail.sender || "Unknown"}
                </p>

                <p>
                  <strong>Date:</strong>{" "}
                  {formatActivity(
                    selectedEmail.internal_date,
                    selectedEmail.received_at
                  )}
                </p>

                {selectedEmail.analysis_status &&
                  selectedEmail.analysis_status !== "done" && (
                    <p className="inbox-analysis-status">
                      Analysis: {selectedEmail.analysis_status}
                    </p>
                  )}

                {threadMessages.length > 1 && (
                  <div className="thread-messages">
                    <h3>Thread ({threadMessages.length})</h3>
                    <ul>
                      {threadMessages.map((msg) => (
                        <li key={msg.id}>
                          <button
                            type="button"
                            className={
                              selectedEmail.id === msg.id ? "active" : ""
                            }
                            onClick={() => setSelectedEmail(msg)}
                          >
                            {msg.sender || "Unknown"} —{" "}
                            {formatActivity(msg.internal_date, msg.received_at)}
                          </button>
                        </li>
                      ))}
                    </ul>
                  </div>
                )}

                <hr />

                <h3>AI Summary</h3>

                <p>{analysis?.summary || "No analysis available yet"}</p>

                <div className="analysis-info">
                  <p>
                    <strong>Category:</strong>{" "}
                    {analysis?.category || "N/A"}
                  </p>

                  <p>
                    <strong>Importance:</strong>{" "}
                    {analysis?.importance || "N/A"}
                  </p>

                  <p>
                    <strong>Action Required:</strong>{" "}
                    {analysis
                      ? analysis.action_required
                        ? "Yes"
                        : "No"
                      : "N/A"}
                  </p>
                </div>

                <h3>Deadlines</h3>

                {analysis?.deadlines?.length ? (
                  <ul>
                    {analysis.deadlines.map((deadline, index) => (
                      <li key={index}>{deadline}</li>
                    ))}
                  </ul>
                ) : (
                  <p>No deadlines detected.</p>
                )}

                <h3>Email Preview</h3>

                <p className="body-preview">
                  {selectedEmail.snippet || "No preview available"}
                </p>

                <DraftPanel
                  key={selectedEmail.id}
                  emailId={selectedEmail.id}
                  sender={selectedEmail.sender}
                />
              </>
            )}

            <ChatPanel emailId={selectedEmail?.id} />
          </main>
        </div>
      )}
    </div>
  );
}
