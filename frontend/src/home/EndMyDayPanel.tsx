import { useEffect, useState } from "react";
import { API } from "../api";

type ActionRow = {
  recommendation_key?: string | null;
  source_email_id?: string | null;
  gmail_thread_id?: string | null;
  subject?: string | null;
  title?: string | null;
  sender?: string | null;
  recipient?: string | null;
  waiting_hours?: number;
  priority?: string | null;
  actions_enabled?: boolean;
  due_date?: string | null;
  overdue?: boolean;
  reason?: string | null;
  review_status?: string | null;
};

type Decision = {
  action: string;
  recommendation_key?: string | null;
  source_email_id?: string | null;
  at: string;
};

type Checkpoint = {
  closed_at?: string;
  summary?: string;
  local_date?: string;
  unresolved?: unknown[];
};

export type EndMyDayReview = {
  enabled?: boolean;
  local_date?: string;
  timezone?: string;
  wrapup_source?: string;
  summary_text?: string;
  verified_counts?: string;
  metrics?: {
    emails_received?: number;
    incoming_awaiting_user?: number;
    outgoing_awaiting_recipient?: number;
    deadlines?: number;
    unresolved?: number;
  };
  incoming_awaiting_user?: ActionRow[];
  needs_review?: ActionRow[];
  outgoing_awaiting_recipient?: ActionRow[];
  deadlines?: ActionRow[];
  unresolved_items?: ActionRow[];
  data_freshness?: {
    message?: string;
    last_sync_at?: string | null;
    newest_stored_at?: string | null;
    stale?: boolean;
    finish_allowed?: boolean;
    finish_block_reason?: string;
    historical_backfill_complete?: boolean;
    complete?: boolean;
    window_covered?: boolean;
  };
  checkpoint?: Checkpoint | null;
  checkpoint_note?: string | null;
  message?: string;
};

type EndMyDayPanelProps = {
  open: boolean;
  onClose: () => void;
  onOpenEmail: (
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
  ) => void;
};

function formatWhen(iso?: string | null) {
  if (!iso) return "";
  try {
    return new Date(iso).toLocaleString(undefined, {
      month: "short",
      day: "numeric",
      hour: "numeric",
      minute: "2-digit",
    });
  } catch {
    return iso;
  }
}

export default function EndMyDayPanel({
  open,
  onClose,
  onOpenEmail,
}: EndMyDayPanelProps) {
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const [savedMessage, setSavedMessage] = useState("");
  const [review, setReview] = useState<EndMyDayReview | null>(null);
  const [decisions, setDecisions] = useState<Decision[]>([]);
  const [busyKey, setBusyKey] = useState<string | null>(null);
  const [refreshing, setRefreshing] = useState(false);
  const [refreshNote, setRefreshNote] = useState("");

  useEffect(() => {
    if (!open) return;
    setDecisions([]);
    setSavedMessage("");
    setError("");
    setRefreshNote("");
    void loadReview();
  }, [open]);

  async function loadReview(fresh = false) {
    setLoading(true);
    setError("");
    try {
      const response = await fetch(
        fresh
          ? `${API}/db/end-my-day/review?fresh=true`
          : `${API}/db/end-my-day/review`
      );
      const data = (await response.json().catch(() => null)) as
        | (EndMyDayReview & { detail?: string })
        | null;
      if (!response.ok) {
        throw new Error(
          data && typeof data.detail === "string"
            ? data.detail
            : "Unable to load End My Day."
        );
      }
      setReview(data);
    } catch (err) {
      setReview(null);
      setError(err instanceof Error ? err.message : "Unable to load End My Day.");
    } finally {
      setLoading(false);
    }
  }

  function remember(
    action: string,
    row: { recommendation_key?: string | null; source_email_id?: string | null }
  ) {
    setDecisions((prev) => [
      ...prev,
      {
        action,
        recommendation_key: row.recommendation_key,
        source_email_id: row.source_email_id,
        at: new Date().toISOString(),
      },
    ]);
  }

  async function draftReply(row: ActionRow) {
    const emailId = row.source_email_id;
    if (!emailId || busyKey) return;
    setBusyKey(`reply:${emailId}`);
    setError("");
    try {
      const response = await fetch(`${API}/emails/${emailId}/draft`, {
        method: "POST",
      });
      const data = await response.json().catch(() => ({}));
      if (!response.ok) {
        throw new Error(
          typeof data.detail === "string" ? data.detail : "Draft failed"
        );
      }
      remember("draft_reply", row);
      onOpenEmail(emailId, {
        tab: "draft",
        draft:
          data && typeof data.id === "string" && typeof data.email_id === "string"
            ? data
            : null,
      });
    } catch (err) {
      setError(err instanceof Error ? err.message : "Draft failed");
    } finally {
      setBusyKey(null);
    }
  }

  async function runFollowup(
    action: "draft" | "remind" | "dismiss",
    row: ActionRow
  ) {
    const key = row.recommendation_key;
    if (!key || busyKey) return;
    setBusyKey(`${action}:${key}`);
    setError("");
    try {
      if (action === "draft") {
        const response = await fetch(
          `${API}/db/briefings/recommendations/${encodeURIComponent(key)}/draft`,
          { method: "POST" }
        );
        const data = await response.json().catch(() => ({}));
        if (!response.ok) {
          throw new Error(
            typeof data.detail === "string" ? data.detail : "Draft failed"
          );
        }
        remember("draft_followup", row);
        if (data.ok && data.draft?.email_id) {
          onOpenEmail(data.draft.email_id, { tab: "draft", draft: data.draft });
        } else if (row.source_email_id) {
          onOpenEmail(row.source_email_id, { tab: "draft" });
        }
        return;
      }
      if (action === "remind") {
        const response = await fetch(
          `${API}/db/briefings/recommendations/${encodeURIComponent(key)}/snooze`,
          {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ preset: "tomorrow" }),
          }
        );
        const data = await response.json().catch(() => ({}));
        if (!response.ok) {
          throw new Error(
            typeof data.detail === "string" ? data.detail : "Remind failed"
          );
        }
        remember("remind_tomorrow", row);
        await loadReview(true);
        return;
      }
      const response = await fetch(
        `${API}/db/briefings/recommendations/${encodeURIComponent(key)}/status`,
        {
          method: "PATCH",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ status: "dismissed" }),
        }
      );
      const data = await response.json().catch(() => ({}));
      if (!response.ok) {
        throw new Error(
          typeof data.detail === "string" ? data.detail : "Dismiss failed"
        );
      }
      remember("dismiss", row);
      await loadReview(true);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Action failed");
    } finally {
      setBusyKey(null);
    }
  }

  async function refreshMailbox() {
    if (refreshing || loading) return;
    setRefreshing(true);
    setError("");
    setRefreshNote("");
    try {
      const response = await fetch(`${API}/db/end-my-day/refresh`, { method: "POST" });
      const data = (await response.json().catch(() => null)) as
        | (EndMyDayReview & {
            detail?: string;
            ok?: boolean;
            status?: string;
            message?: string;
            review?: EndMyDayReview | null;
          })
        | null;
      if (!response.ok) {
        throw new Error(
          data && typeof data.detail === "string"
            ? data.detail
            : "Mailbox refresh failed. The closing review was not updated."
        );
      }
      if (data?.review) setReview(data.review);
      if (!data || data.ok === false || data.status === "failed") {
        setError(
          data?.message ||
            "Mailbox refresh failed. The closing review was not updated."
        );
        return;
      }
      setRefreshNote(
        data.message ||
          (data.status === "partial"
            ? "Refresh finished, but today's mailbox coverage is still incomplete."
            : "Mailbox refreshed. Today's review is updated.")
      );
    } catch (err) {
      setError(
        err instanceof Error
          ? err.message
          : "Mailbox refresh failed. The closing review was not updated."
      );
    } finally {
      setRefreshing(false);
    }
  }

  async function finishDay() {
    if (saving || refreshing || review?.data_freshness?.finish_allowed !== true) return;
    setSaving(true);
    setError("");
    try {
      const response = await fetch(`${API}/db/end-my-day/finish`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ decisions }),
      });
      const data = await response.json().catch(() => ({}));
      if (!response.ok) {
        throw new Error(
          typeof data.detail === "string"
            ? data.detail
            : "Unable to save today's review."
        );
      }
      const closed = data?.checkpoint?.closed_at as string | undefined;
      setSavedMessage(
        closed
          ? `Today's review was saved at ${formatWhen(closed)}.`
          : "Today's review was saved."
      );
      setReview((prev) =>
        prev
          ? { ...prev, checkpoint: data.checkpoint || prev.checkpoint }
          : prev
      );
    } catch (err) {
      setError(err instanceof Error ? err.message : "Unable to save today's review.");
    } finally {
      setSaving(false);
    }
  }

  if (!open) return null;

  const checkpoint = review?.checkpoint;
  const freshness = review?.data_freshness;

  return (
    <div className="catchup-overlay" role="dialog" aria-modal="true" aria-label="End My Day">
      <div className="catchup-panel">
        <div className="catchup-panel-head">
          <div>
            <h3>End My Day</h3>
            <p>Review today and save a closing checkpoint.</p>
          </div>
          <div className="catchup-head-actions">
            <button
              type="button"
              className="catchup-close"
              disabled={refreshing || loading}
              onClick={() => void refreshMailbox()}
            >
              {refreshing ? "Refreshing mailbox…" : "Refresh mailbox"}
            </button>
            <button type="button" className="catchup-close" onClick={onClose}>
              Close
            </button>
          </div>
        </div>

        {loading ? <p className="briefing-empty-line">Loading today’s review…</p> : null}
        {refreshing ? (
          <p className="briefing-empty-line">Refreshing mailbox and waiting for analysis…</p>
        ) : null}
        {refreshNote ? <p className="catchup-freshness">{refreshNote}</p> : null}
        {error ? (
          <p className="catchup-error" role="alert">
            {error}
          </p>
        ) : null}
        {savedMessage ? <p className="catchup-freshness">{savedMessage}</p> : null}
        {checkpoint?.closed_at && !savedMessage ? (
          <p className="catchup-freshness">
            Today&apos;s review was saved at {formatWhen(checkpoint.closed_at)}.
            Unresolved work is still listed below.
          </p>
        ) : null}
        {review?.checkpoint_note ? (
          <p className="catchup-freshness">{review.checkpoint_note}</p>
        ) : null}

        {review ? (
          <>
            <section className="catchup-section">
              <h4>Closing summary</h4>
              <p className="catchup-summary">{review.summary_text}</p>
              <p className="catchup-freshness">
                {review.wrapup_source === "evening_wrapup"
                  ? "Narrative from today’s saved Evening Wrap-up. "
                  : "No Evening Wrap-up for today. Summary is from stored mail. "}
                {freshness?.message}{" "}
                {freshness?.last_sync_at
                  ? ` Last incremental sync ${formatWhen(freshness.last_sync_at)}.`
                  : ""}
                {freshness?.newest_stored_at
                  ? ` Newest stored mail ${formatWhen(freshness.newest_stored_at)}.`
                  : ""}
              </p>
            </section>

            <ActionSection
              title="Incoming requests awaiting you"
              empty="No incoming requests are waiting on your reply."
              rows={review.incoming_awaiting_user || []}
              busyKey={busyKey}
              onOpen={(row) => {
                if (row.source_email_id) onOpenEmail(row.source_email_id);
              }}
              onDraftReply={(row) => void draftReply(row)}
              showReply
            />
            <ActionSection
              title="Needs review"
              empty="No ambiguous messages need review."
              rows={review.needs_review || []}
              busyKey={busyKey}
              onOpen={(row) => {
                if (row.source_email_id) onOpenEmail(row.source_email_id);
              }}
              onDraftReply={(row) => void draftReply(row)}
              showReply
            />
            <ActionSection
              title="Outgoing requests awaiting them"
              empty="No outgoing requests are waiting on a recipient."
              rows={review.outgoing_awaiting_recipient || []}
              busyKey={busyKey}
              onOpen={(row) => {
                if (row.source_email_id) onOpenEmail(row.source_email_id);
              }}
              onFollowup={(action, row) => void runFollowup(action, row)}
            />

            <section className="catchup-section">
              <h4>Upcoming deadlines</h4>
              {(review.deadlines || []).length === 0 ? (
                <p className="briefing-empty-line">No upcoming or overdue deadlines.</p>
              ) : (
                <ul className="catchup-list">
                  {(review.deadlines || []).map((row, index) => (
                    <li key={`${row.source_email_id}-${row.due_date}-${index}`}>
                      <button
                        type="button"
                        className="catchup-item"
                        disabled={!row.source_email_id}
                        onClick={() =>
                          row.source_email_id && onOpenEmail(row.source_email_id)
                        }
                      >
                        <strong>
                          {row.overdue ? "Overdue · " : ""}
                          {row.due_date ? `${row.due_date} · ` : ""}
                          {row.title}
                        </strong>
                      </button>
                    </li>
                  ))}
                </ul>
              )}
            </section>

            <ActionSection
              title="Unresolved items"
              empty="No other unresolved items for today."
              rows={review.unresolved_items || []}
              busyKey={busyKey}
              onOpen={(row) => {
                if (row.source_email_id) onOpenEmail(row.source_email_id);
              }}
              onDraftReply={(row) => void draftReply(row)}
              showReply
            />

            <section className="catchup-section">
              <button
                type="button"
                className="home-catchup-btn"
                disabled={
                  saving ||
                  loading ||
                  refreshing ||
                  freshness?.finish_allowed !== true
                }
                onClick={() => void finishDay()}
              >
                {saving ? "Saving…" : "Finish My Day"}
              </button>
              {freshness?.finish_allowed !== true ? (
                <p className="catchup-freshness">
                  {freshness?.finish_block_reason ||
                    freshness?.message ||
                    "Today's mailbox coverage is not verified."}
                </p>
              ) : null}
            </section>
          </>
        ) : null}
      </div>
    </div>
  );
}

function ActionSection({
  title,
  empty,
  rows,
  busyKey,
  onOpen,
  onDraftReply,
  onFollowup,
  showReply = false,
}: {
  title: string;
  empty: string;
  rows: ActionRow[];
  busyKey: string | null;
  onOpen: (row: ActionRow) => void;
  onDraftReply?: (row: ActionRow) => void;
  onFollowup?: (action: "draft" | "remind" | "dismiss", row: ActionRow) => void;
  showReply?: boolean;
}) {
  return (
    <section className="catchup-section">
      <h4>{title}</h4>
      {rows.length === 0 ? (
        <p className="briefing-empty-line">{empty}</p>
      ) : (
        <ul className="catchup-list">
          {rows.map((row, index) => (
            <li key={`${row.recommendation_key || row.source_email_id}-${index}`}>
              <div className="catchup-item static">
                <strong>{row.title || row.subject}</strong>
                <div className="end-my-day-parties">
                  {row.sender ? <span>From: {row.sender}</span> : null}
                  {row.recipient ? <span>To: {row.recipient}</span> : null}
                  {row.waiting_hours ? <span>{row.waiting_hours}h waiting</span> : null}
                </div>
                {row.reason ? <span>{row.reason}</span> : null}
                <div className="briefing-followup-actions">
                  {row.source_email_id ? (
                    <button
                      type="button"
                      className="briefing-followup-btn"
                      onClick={() => onOpen(row)}
                    >
                      View Email
                    </button>
                  ) : null}
                  {showReply && row.source_email_id && onDraftReply ? (
                    <button
                      type="button"
                      className="briefing-followup-btn"
                      disabled={Boolean(busyKey)}
                      onClick={() => onDraftReply(row)}
                    >
                      Draft Reply
                    </button>
                  ) : null}
                  {onFollowup && row.actions_enabled && row.recommendation_key ? (
                    <>
                      <button
                        type="button"
                        className="briefing-followup-btn"
                        disabled={Boolean(busyKey)}
                        onClick={() => onFollowup("draft", row)}
                      >
                        Draft Follow-up
                      </button>
                      <button
                        type="button"
                        className="briefing-followup-btn"
                        disabled={Boolean(busyKey)}
                        onClick={() => onFollowup("remind", row)}
                      >
                        Remind Tomorrow
                      </button>
                      <button
                        type="button"
                        className="briefing-followup-btn muted"
                        disabled={Boolean(busyKey)}
                        onClick={() => onFollowup("dismiss", row)}
                      >
                        Dismiss
                      </button>
                    </>
                  ) : null}
                </div>
              </div>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
