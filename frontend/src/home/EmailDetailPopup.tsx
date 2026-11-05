import { useEffect, useMemo, useState } from "react";
import { createPortal } from "react-dom";
import DraftPanel from "../DraftPanel";
import EmailGlanceTabs from "./EmailGlanceTabs";
import EmailReading from "./EmailReading";
import { extractMeetingUrl } from "./meetingLink";

export type PopupEmailAnalysis = {
  summary: string;
  category: string;
  importance: string;
  deadlines: string[];
  action_required: boolean;
  confidence: number;
};

export type PopupEmail = {
  id: string;
  gmail_thread_id?: string | null;
  subject: string | null;
  sender: string | null;
  received_at: string | null;
  snippet: string | null;
  body?: string | null;
  internal_date?: number | null;
  analysis_status?: string;
  email_analyses?: PopupEmailAnalysis[] | PopupEmailAnalysis | null;
};

type MailTab = "summary" | "thread" | "draft";

type EmailDetailPopupProps = {
  open: boolean;
  onClose: () => void;
  selectedEmail: PopupEmail | null;
  threadMessages: PopupEmail[];
  onSelectThreadMessage: (email: PopupEmail) => void;
  detailLoading: boolean;
  detailError: string;
  analysis: PopupEmailAnalysis | null;
  analysisPending: boolean;
  analysisFailed: boolean;
  analysisError?: string;
  onRequestAnalysis?: () => void;
  formatTime: (
    internalDate: number | null | undefined,
    fallback: string | null
  ) => string;
  /** When opening from chat "Open draft", land on the Draft tab. */
  initialTab?: MailTab;
  /** Chatbot-created draft to show on the Draft tab with the same body. */
  initialDraft?: {
    id: string;
    email_id: string;
    body: string;
    status: string;
  } | null;
};

function parseSender(raw: string | null | undefined): {
  name: string;
  email: string | null;
  initial: string;
} {
  const value = (raw || "Unknown").trim();
  const match = value.match(/^(.*?)\s*<([^>]+)>$/);
  const name = (match?.[1] || value).replace(/^"|"$/g, "").trim() || "Unknown";
  const email = match?.[2]?.trim() || null;
  const initial = name.charAt(0).toUpperCase() || "?";
  return { name, email, initial };
}

function previewText(email: PopupEmail): string {
  const body = email.body?.trim();
  if (body) {
    return body.length > 420 ? `${body.slice(0, 420).trimEnd()}…` : body;
  }
  return email.snippet?.trim() || "No preview available";
}

function fullEmailText(email: PopupEmail): string {
  const raw = email.body?.trim() || email.snippet?.trim() || "";
  if (!raw) return "No email content available";
  return raw
    .replace(/\r\n/g, "\n")
    .replace(/[ \t]+\n/g, "\n")
    .replace(/\n[ \t]+/g, "\n")
    .replace(/\n{3,}/g, "\n\n")
    .trim();
}

export default function EmailDetailPopup({
  open,
  onClose,
  selectedEmail,
  threadMessages,
  onSelectThreadMessage,
  detailLoading,
  detailError,
  analysis,
  analysisPending,
  analysisFailed,
  analysisError,
  onRequestAnalysis,
  formatTime,
  initialTab,
  initialDraft = null,
}: EmailDetailPopupProps) {
  const [tab, setTab] = useState<MailTab>(initialTab || "summary");
  const [autoGenerateToken, setAutoGenerateToken] = useState(0);

  const conversationKey =
    selectedEmail?.gmail_thread_id || selectedEmail?.id || "";
  const glanceEmail = selectedEmail
    ? {
        sender: selectedEmail.sender,
        subject: selectedEmail.subject,
        snippet: selectedEmail.snippet,
        body: selectedEmail.body,
        email_analyses: analysis ?? selectedEmail.email_analyses,
      }
    : null;
  const meetingUrl = useMemo(
    () =>
      selectedEmail
        ? extractMeetingUrl(
            selectedEmail.subject,
            selectedEmail.snippet,
            selectedEmail.body
          )
        : null,
    [selectedEmail]
  );

  useEffect(() => {
    if (!open) return;

    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";

    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        onClose();
      }
    };

    document.addEventListener("keydown", onKeyDown);
    return () => {
      document.body.style.overflow = previousOverflow;
      document.removeEventListener("keydown", onKeyDown);
    };
  }, [open, onClose]);

  useEffect(() => {
    if (!open) return;
    setTab(initialTab || "summary");
  }, [open, conversationKey, initialTab]);

  useEffect(() => {
    setAutoGenerateToken(0);
  }, [selectedEmail?.id]);

  if (!open) return null;

  const insightValue = (value: string | null | undefined) =>
    value || (analysisPending ? "…" : "N/A");

  function openDraftAndGenerate() {
    setTab("draft");
    setAutoGenerateToken((current) => current + 1);
  }

  function selectThreadMessage(email: PopupEmail) {
    onSelectThreadMessage(email);
    setTab("summary");
  }

  return createPortal(
    <div
      className="home-mail-overlay"
      onClick={onClose}
      role="presentation"
    >
      <div
        className="home-mail-dialog"
        role="dialog"
        aria-modal="true"
        aria-labelledby="home-mail-dialog-title"
        onClick={(event) => event.stopPropagation()}
      >
        <div className="home-mail-dialog-head">
          <div className="home-mail-dialog-title">
            <span className="home-mail-dialog-icon" aria-hidden>
              ✉
            </span>
            <div>
              <h2 id="home-mail-dialog-title">AI Summary & Draft</h2>
              <p>Selected conversation details</p>
            </div>
          </div>
          <button
            type="button"
            className="home-mail-close"
            onClick={onClose}
            aria-label="Close email details"
          >
            ×
          </button>
        </div>

        <div className="home-mail-dialog-body">
          {detailLoading ? (
            <p className="home-empty">Loading email details…</p>
          ) : detailError ? (
            <p className="home-error">{detailError}</p>
          ) : !selectedEmail ? (
            <div className="home-empty-state">
              <div className="home-empty-illus" aria-hidden>
                ✉
              </div>
              <h3>Select a conversation</h3>
              <p>
                Choose an email from the inbox to see summary, insights and
                suggested actions.
              </p>
            </div>
          ) : (
            <>
              <div className="home-mail-glance-head">
                <h3 className="home-mail-subject">
                  {selectedEmail.subject || "No Subject"}
                </h3>
                {glanceEmail ? <EmailGlanceTabs email={glanceEmail} /> : null}
              </div>
              <div className="home-mail-tabs" role="tablist" aria-label="Mail details">
                <button
                  type="button"
                  role="tab"
                  id="home-mail-tab-summary"
                  aria-selected={tab === "summary"}
                  aria-controls="home-mail-panel-summary"
                  className={`home-mail-tab${tab === "summary" ? " active" : ""}`}
                  onClick={() => setTab("summary")}
                >
                  <span aria-hidden>✦</span> AI Summary
                </button>
                <button
                  type="button"
                  role="tab"
                  id="home-mail-tab-thread"
                  aria-selected={tab === "thread"}
                  aria-controls="home-mail-panel-thread"
                  className={`home-mail-tab${tab === "thread" ? " active" : ""}`}
                  onClick={() => setTab("thread")}
                >
                  <span aria-hidden>☰</span> Email Thread
                </button>
                <button
                  type="button"
                  role="tab"
                  id="home-mail-tab-draft"
                  aria-selected={tab === "draft"}
                  aria-controls="home-mail-panel-draft"
                  className={`home-mail-tab${tab === "draft" ? " active" : ""}`}
                  onClick={() => setTab("draft")}
                >
                  <span aria-hidden>✎</span> Draft Reply
                </button>
              </div>

              <div
                id="home-mail-panel-summary"
                role="tabpanel"
                aria-labelledby="home-mail-tab-summary"
                hidden={tab !== "summary"}
                className="home-mail-panel"
              >
                <div className="home-ai-box">
                  <h3>
                    <span aria-hidden>✦</span> AI Summary
                    {glanceEmail ? (
                      <EmailGlanceTabs email={glanceEmail} />
                    ) : null}
                  </h3>
                  <p>
                    {analysis?.summary ||
                      (analysisPending
                        ? "Analyzing this email…"
                        : analysisFailed
                          ? "Analysis failed. You can retry, or wait for the next recent-mail sync."
                          : "No AI summary yet. Generate one when you need it — the original email is below.")}
                  </p>
                  {meetingUrl ? (
                    <p className="home-meeting-link">
                      <span className="home-meeting-link-label">Meeting link</span>
                      <a
                        href={meetingUrl}
                        target="_blank"
                        rel="noopener noreferrer"
                      >
                        {meetingUrl}
                      </a>
                    </p>
                  ) : null}
                  {analysisError ? (
                    <p className="home-error">{analysisError}</p>
                  ) : null}
                  {!analysis && onRequestAnalysis ? (
                    <button
                      type="button"
                      className="home-mail-generate"
                      onClick={onRequestAnalysis}
                      disabled={analysisPending}
                    >
                      <span aria-hidden>✦</span>{" "}
                      {analysisFailed ? "Retry AI Summary" : "Generate AI Summary"}
                    </button>
                  ) : null}
                </div>

                <div className="home-analysis-grid home-mail-insights">
                  <div className="home-mail-insight labels">
                    <span className="home-mail-insight-label">
                      <span aria-hidden>🏷️</span> Labels
                    </span>
                    {glanceEmail ? (
                      <EmailGlanceTabs email={glanceEmail} />
                    ) : (
                      <strong>{insightValue(null)}</strong>
                    )}
                  </div>
                  <div className="home-mail-insight action">
                    <span className="home-mail-insight-label">
                      <span aria-hidden>✓</span> Action required
                    </span>
                    <strong>
                      {analysis
                        ? analysis.action_required
                          ? "Yes"
                          : "No"
                        : insightValue(null)}
                    </strong>
                  </div>
                </div>

                <details className="home-mail-original" open>
                  <summary>Original Email (Latest)</summary>
                  <EmailReading text={fullEmailText(selectedEmail)} />
                </details>

                <button
                  type="button"
                  className="home-mail-generate"
                  onClick={openDraftAndGenerate}
                >
                  <span aria-hidden>✎</span> Generate Draft
                </button>
              </div>

              <div
                id="home-mail-panel-thread"
                role="tabpanel"
                aria-labelledby="home-mail-tab-thread"
                hidden={tab !== "thread"}
                className="home-mail-panel"
              >
                <p className="home-mail-thread-label">
                  Email Thread ({threadMessages.length || 1})
                </p>
                <ul className="home-mail-thread-list">
                  {(threadMessages.length ? threadMessages : [selectedEmail]).map(
                    (msg) => {
                      const sender = parseSender(msg.sender);
                      const active = selectedEmail.id === msg.id;
                      return (
                        <li key={msg.id}>
                          <button
                            type="button"
                            className={`home-mail-thread-row${active ? " active" : ""}`}
                            onClick={() => selectThreadMessage(msg)}
                          >
                            <span className="home-avatar" aria-hidden>
                              {sender.initial}
                            </span>
                            <span className="home-mail-thread-body">
                              <span className="home-mail-thread-top">
                                <strong>{sender.name}</strong>
                                <time>
                                  {formatTime(msg.internal_date, msg.received_at)}
                                </time>
                              </span>
                              {sender.email && (
                                <span className="home-mail-thread-email">
                                  {sender.email}
                                </span>
                              )}
                              <span className="home-mail-thread-snippet">
                                {msg.snippet || previewText(msg)}
                              </span>
                            </span>
                          </button>
                        </li>
                      );
                    }
                  )}
                </ul>
              </div>

              <div
                id="home-mail-panel-draft"
                role="tabpanel"
                aria-labelledby="home-mail-tab-draft"
                hidden={tab !== "draft"}
                className="home-mail-panel"
              >
                <DraftPanel
                  key={`${selectedEmail.id}:${initialDraft?.id || "none"}`}
                  emailId={selectedEmail.id}
                  sender={selectedEmail.sender}
                  embedded
                  autoGenerateToken={tab === "draft" ? autoGenerateToken : 0}
                  initialDraft={
                    initialDraft && initialDraft.email_id === selectedEmail.id
                      ? initialDraft
                      : null
                  }
                />
                <details className="home-mail-original">
                  <summary>Original Email (Latest)</summary>
                  <EmailReading text={fullEmailText(selectedEmail)} />
                </details>
              </div>
            </>
          )}
        </div>
      </div>
    </div>,
    document.body
  );
}
