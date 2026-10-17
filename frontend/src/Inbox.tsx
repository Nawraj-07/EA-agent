import { useCallback, useEffect, useState } from "react";
import "./Inbox.css";
import ChatPanel from "./ChatPanel";
import DraftPanel from "./DraftPanel";
import BriefingPanel from "./BriefingPanel";

const API = import.meta.env.VITE_API_URL || "http://localhost:8000";
const AUTO_REFRESH_MS = 60_000;

type EmailAnalysis = {
  summary: string;
  category: string;
  importance: string;
  deadlines: string[];
  action_required: boolean;
  confidence: number;
};

type Email = {
  id: string;
  gmail_message_id: string;
  subject: string | null;
  sender: string | null;
  received_at: string | null;
  snippet: string | null;
  body?: string | null;
  email_analyses?: EmailAnalysis[] | EmailAnalysis | null;
};

function getAnalysis(email: Email | null): EmailAnalysis | null {
  if (!email) return null;

  const analyses = email.email_analyses;

  if (Array.isArray(analyses)) {
    return analyses[0] ?? null;
  }

  return analyses ?? null;
}

export default function Inbox() {
  const [emails, setEmails] = useState<Email[]>([]);
  const [selectedEmail, setSelectedEmail] = useState<Email | null>(null);

  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [detailLoading, setDetailLoading] = useState(false);
  const [error, setError] = useState("");
  const [detailError, setDetailError] = useState("");
  const [lastRefreshedAt, setLastRefreshedAt] = useState<Date | null>(null);

  const loadEmails = useCallback(async (options?: { silent?: boolean }) => {
    const silent = options?.silent ?? false;

    try {
      if (silent) {
        setRefreshing(true);
      } else {
        setLoading(true);
      }
      setError("");

      const response = await fetch(`${API}/db/emails`);

      if (!response.ok) {
        throw new Error(`Failed to load emails (${response.status})`);
      }

      const data: Email[] = await response.json();

      if (!Array.isArray(data)) {
        throw new Error("Unexpected API response");
      }

      setEmails(data);
      setLastRefreshedAt(new Date());
    } catch (err) {
      setError(
        err instanceof Error ? err.message : "Something went wrong"
      );
    } finally {
      setLoading(false);
      setRefreshing(false);
    }
  }, []);

  useEffect(() => {
    void loadEmails();
  }, [loadEmails]);

  useEffect(() => {
    const timer = window.setInterval(() => {
      void loadEmails({ silent: true });
    }, AUTO_REFRESH_MS);

    return () => window.clearInterval(timer);
  }, [loadEmails]);

  async function selectEmail(emailId: string) {
    setDetailLoading(true);
    setDetailError("");
    setSelectedEmail(null);

    try {
      const response = await fetch(`${API}/db/emails/${emailId}`);

      if (!response.ok) {
        throw new Error(
          `Failed to load email details (${response.status})`
        );
      }

      const data: Email = await response.json();

      setSelectedEmail(data);
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
          <p>Your saved emails and AI-powered insights</p>
          {refreshedLabel && (
            <p className="inbox-refreshed">
              Last refreshed {refreshedLabel}
              {refreshing ? " · updating…" : ""}
            </p>
          )}
        </div>

        <div className="inbox-header-actions">
          <button
            type="button"
            className="inbox-refresh-btn"
            onClick={() => void loadEmails({ silent: true })}
            disabled={loading || refreshing}
          >
            {refreshing ? "Refreshing…" : "Refresh inbox"}
          </button>
          <span className="email-count">
            {emails.length} Emails
          </span>
        </div>
      </header>

      {error && (
        <div className="inbox-error">
          {error}
        </div>
      )}

      <BriefingPanel />

      {loading ? (
        <p className="inbox-message">Loading emails...</p>
      ) : error ? null : emails.length === 0 ? (
        <p className="inbox-message">
          No saved emails yet. New mail is synced automatically every 15
          minutes, or click Refresh inbox after a sync.
        </p>
      ) : (
        <div className="inbox-layout">
          <aside className="email-list">
            <h2>Saved Emails</h2>

            {emails.map((email) => {
              const itemAnalysis = getAnalysis(email);

              return (
                <button
                  key={email.id}
                  className={`email-item ${
                    selectedEmail?.id === email.id ? "active" : ""
                  }`}
                  onClick={() => selectEmail(email.id)}
                >
                  <div className="email-item-top">
                    <strong>
                      {email.subject || "No Subject"}
                    </strong>

                    <span
                      className={`importance ${(
                        itemAnalysis?.importance || "low"
                      ).toLowerCase()}`}
                    >
                      {itemAnalysis?.importance || "N/A"}
                    </span>
                  </div>

                  <p>{email.sender || "Unknown Sender"}</p>

                  <small>
                    {email.snippet || "No preview available"}
                  </small>
                </button>
              );
            })}
          </aside>

          <main className="email-detail">
            {detailLoading ? (
              <p>Loading email details...</p>
            ) : detailError ? (
              <p className="inbox-error">{detailError}</p>
            ) : !selectedEmail ? (
              <div className="empty-detail">
                Select an email to view its AI analysis.
              </div>
            ) : (
              <>
                <h2>
                  {selectedEmail.subject || "No Subject"}
                </h2>

                <p>
                  <strong>From:</strong>{" "}
                  {selectedEmail.sender || "Unknown"}
                </p>

                <p>
                  <strong>Date:</strong>{" "}
                  {selectedEmail.received_at || "Unknown"}
                </p>

                <hr />

                <h3>AI Summary</h3>

                <p>
                  {analysis?.summary || "No analysis available"}
                </p>

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
