import { useEffect, useRef, useState } from "react";
import { API } from "../api";

export type CatchupPayload = {
  enabled?: boolean;
  cached?: boolean;
  window?: {
    start?: string;
    end?: string;
    start_local?: string;
    end_local?: string;
    timezone?: string;
    source?: string;
    first_visit?: boolean;
    default_lookback_hours?: number;
  };
  metrics?: {
    emails_received?: number;
    unread?: number;
    need_attention?: number;
    deadlines?: number;
    waiting_for_replies?: number;
    incoming_awaiting_user?: number;
    outgoing_awaiting_recipient?: number;
  };
  important_senders?: string[];
  top_priorities?: Array<{
    source_email_id?: string;
    title?: string;
    subject?: string;
    sender?: string;
    reason?: string;
    priority?: string;
  }>;
  deadlines?: Array<{
    title?: string;
    due_date?: string;
    source_email_id?: string;
    priority?: string;
    confirmed?: boolean;
  }>;
  followups?: {
    incoming_awaiting_user?: Array<{
      recommendation_key?: string;
      source_email_id?: string;
      subject?: string;
      sender?: string;
      recipient?: string;
      waiting_hours?: number;
      priority?: string;
      title?: string;
      actions_enabled?: boolean;
    }>;
    outgoing_awaiting_recipient?: Array<{
      recommendation_key?: string;
      source_email_id?: string;
      subject?: string;
      recipient?: string;
      waiting_hours?: number;
      priority?: string;
      title?: string;
      actions_enabled?: boolean;
    }>;
  };
  important_updates?: string[];
  summary_text?: string;
  data_freshness?: {
    message?: string;
    stale?: boolean;
    coverage?: string;
    truncated?: boolean;
    last_sync_at?: string | null;
  };
};

type CatchupPanelProps = {
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

function formatLocal(iso?: string) {
  if (!iso) return "";
  try {
    return new Date(iso).toLocaleString(undefined, {
      weekday: "short",
      month: "short",
      day: "numeric",
      hour: "numeric",
      minute: "2-digit",
    });
  } catch {
    return iso;
  }
}

function formatDateTimeLocalValue(value: string) {
  if (!value) return "";
  try {
    // datetime-local is timezone-naive; treat as local wall clock
    const parsed = new Date(value);
    if (Number.isNaN(parsed.getTime())) return "";
    return parsed.toLocaleString(undefined, {
      weekday: "short",
      month: "short",
      day: "numeric",
      year: "numeric",
      hour: "numeric",
      minute: "2-digit",
    });
  } catch {
    return "";
  }
}

function toDateTimeLocalValue(iso?: string) {
  if (!iso) return "";
  try {
    const date = new Date(iso);
    if (Number.isNaN(date.getTime())) return "";
    const pad = (n: number) => String(n).padStart(2, "0");
    return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}T${pad(date.getHours())}:${pad(date.getMinutes())}`;
  } catch {
    return "";
  }
}

export default function CatchupPanel({
  open,
  onClose,
  onOpenEmail,
}: CatchupPanelProps) {
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [payload, setPayload] = useState<CatchupPayload | null>(null);
  const [manualStart, setManualStart] = useState("");
  const [expanded, setExpanded] = useState(false);
  const [busyKey, setBusyKey] = useState<string | null>(null);
  const inflight = useRef(false);
  const viewed = useRef(false);

  useEffect(() => {
    if (!open) {
      setExpanded(false);
      return;
    }
    viewed.current = false;
    setError("");
    // Open instantly — do not auto-generate. Prefill a 72h default start.
    setManualStart((prev) => {
      if (prev.trim()) return prev;
      const start = new Date(Date.now() - 72 * 60 * 60 * 1000);
      return toDateTimeLocalValue(start.toISOString());
    });
  }, [open]);

  async function generateCatchup(force = true) {
    if (inflight.current) return;
    inflight.current = true;
    setLoading(true);
    setError("");
    try {
      const body: Record<string, unknown> = { force_refresh: force };
      if (manualStart.trim()) {
        const local = new Date(manualStart);
        if (!Number.isNaN(local.getTime())) {
          body.start = local.toISOString();
        }
      }
      const response = await fetch(`${API}/db/catchup/generate`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      const data = await response.json().catch(() => ({}));
      if (!response.ok) {
        throw new Error(
          typeof data.detail === "string"
            ? data.detail
            : `Catch-up failed (${response.status})`
        );
      }
      const next = data as CatchupPayload;
      setPayload(next);
      if (next.window?.start_local) {
        setManualStart(toDateTimeLocalValue(next.window.start_local));
      }
      viewed.current = true;
    } catch (err) {
      setError(err instanceof Error ? err.message : "Catch-up failed");
    } finally {
      setLoading(false);
      inflight.current = false;
    }
  }

  async function closeAndAck() {
    if (viewed.current) {
      try {
        await fetch(`${API}/db/catchup/record-visit`, { method: "POST" });
      } catch {
        // Non-blocking — visit table may be pending migration
      }
    }
    onClose();
  }

  async function runFollowupAction(
    action: "draft" | "remind" | "dismiss",
    key: string | undefined,
    emailId?: string
  ) {
    if (!key || busyKey) return;
    setBusyKey(`${action}:${key}`);
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
        if (data.ok && data.draft?.email_id) {
          onOpenEmail(data.draft.email_id, {
            tab: "draft",
            draft: data.draft,
          });
        } else if (emailId) {
          onOpenEmail(emailId, { tab: "draft" });
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
        if (!response.ok) {
          const data = await response.json().catch(() => ({}));
          throw new Error(
            typeof data.detail === "string" ? data.detail : "Remind failed"
          );
        }
        void generateCatchup(true);
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
      if (!response.ok) {
        const data = await response.json().catch(() => ({}));
        throw new Error(
          typeof data.detail === "string" ? data.detail : "Dismiss failed"
        );
      }
      void generateCatchup(true);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Action failed");
    } finally {
      setBusyKey(null);
    }
  }

  if (!open) return null;

  const metrics = payload?.metrics;
  const freshness = payload?.data_freshness;
  const timezoneLabel =
    payload?.window?.timezone ||
    Intl.DateTimeFormat().resolvedOptions().timeZone ||
    "local time";
  const selectedPreview = formatDateTimeLocalValue(manualStart);
  const periodStart = formatLocal(payload?.window?.start_local);
  const periodEnd = formatLocal(payload?.window?.end_local);

  return (
    <div
      className={`catchup-overlay${expanded ? " expanded" : ""}`}
      role="dialog"
      aria-modal="true"
      aria-label="What Did I Miss"
    >
      <div className="catchup-panel">
        <div className="catchup-panel-head">
          <div>
            <h3>What Did I Miss?</h3>
            <p>Here&apos;s what happened while you were away.</p>
          </div>
          <div className="catchup-head-actions">
            <button
              type="button"
              className="catchup-icon-btn"
              aria-label={expanded ? "Collapse catch-up window" : "Expand catch-up window"}
              title={expanded ? "Collapse" : "Expand"}
              onClick={() => setExpanded((prev) => !prev)}
            >
              {expanded ? (
                <svg width="16" height="16" viewBox="0 0 24 24" fill="none" aria-hidden>
                  <path
                    d="M9 9H5V5M15 9h4V5M9 15H5v4M15 15h4v4"
                    stroke="currentColor"
                    strokeWidth="2"
                    strokeLinecap="round"
                    strokeLinejoin="round"
                  />
                </svg>
              ) : (
                <svg width="16" height="16" viewBox="0 0 24 24" fill="none" aria-hidden>
                  <path
                    d="M9 3H3v6M15 3h6v6M9 21H3v-6M21 15v6h-6"
                    stroke="currentColor"
                    strokeWidth="2"
                    strokeLinecap="round"
                    strokeLinejoin="round"
                  />
                </svg>
              )}
            </button>
            <button
              type="button"
              className="catchup-close"
              onClick={() => void closeAndAck()}
            >
              Close
            </button>
          </div>
        </div>

        <div className="catchup-window-card">
          <div className="catchup-window-card-top">
            <div>
              <p className="catchup-window-kicker">Catch-up period</p>
              <p className="catchup-window-range">
                {periodStart && periodEnd
                  ? `${periodStart} → ${periodEnd}`
                  : selectedPreview
                    ? `Ready · from ${selectedPreview}`
                    : "Choose when you were last away, then Generate"}
              </p>
              <p className="catchup-window-meta">
                Times shown in {timezoneLabel}
                {payload?.window?.first_visit
                  ? ` · First visit uses a ${payload.window.default_lookback_hours || 72}-hour lookback`
                  : !payload
                    ? " · Default start is 72 hours ago (you can change it)"
                    : null}
                {payload?.cached ? " · Cached result" : null}
              </p>
            </div>
          </div>

          <div className="catchup-toolbar">
            <div className="catchup-away-field">
              <label className="catchup-away-label" htmlFor="catchup-away-since">
                Away since
              </label>
              <p className="catchup-away-hint">
                Choose the date and time Buddy should start from, then click
                Generate.
              </p>
              <div className="catchup-away-controls">
                <input
                  id="catchup-away-since"
                  type="datetime-local"
                  value={manualStart}
                  onChange={(event) => setManualStart(event.target.value)}
                  aria-describedby="catchup-away-preview"
                  disabled={loading}
                />
                <button
                  type="button"
                  className="home-briefing-btn catchup-generate-btn"
                  disabled={loading || !manualStart.trim()}
                  onClick={() => void generateCatchup(true)}
                >
                  {loading
                    ? "Generating…"
                    : payload
                      ? "Regenerate"
                      : "Generate"}
                </button>
              </div>
              <p id="catchup-away-preview" className="catchup-away-preview">
                {selectedPreview
                  ? `Selected start: ${selectedPreview}`
                  : "Tip: pick both a date and a clock time, then press Generate."}
              </p>
            </div>
          </div>
        </div>

        {loading ? (
          <p className="home-empty">Generating your catch-up from stored mail…</p>
        ) : null}
        {!loading && !payload && !error ? (
          <div className="catchup-idle">
            <p className="catchup-idle-title">Ready when you are</p>
            <p className="catchup-idle-copy">
              Set <strong>Away since</strong>, then press <strong>Generate</strong>{" "}
              to see emails, priorities, deadlines, and follow-ups from that period.
            </p>
          </div>
        ) : null}
        {error ? <p className="catchup-error">{error}</p> : null}

        {payload ? (
          <>
            {freshness?.message ? (
              <p className="catchup-freshness">{freshness.message}</p>
            ) : null}

            <div className="catchup-metrics">
              <div className="catchup-metric">
                <strong>{metrics?.emails_received ?? 0}</strong>
                <span>Emails Received</span>
              </div>
              <div className="catchup-metric">
                <strong>{metrics?.need_attention ?? 0}</strong>
                <span>Need Attention</span>
              </div>
              <div className="catchup-metric">
                <strong>{metrics?.deadlines ?? 0}</strong>
                <span>Deadlines</span>
              </div>
              <div className="catchup-metric">
                <strong>{metrics?.waiting_for_replies ?? 0}</strong>
                <span>Waiting for Replies</span>
              </div>
            </div>

            {payload.summary_text ? (
              <p className="catchup-summary">{payload.summary_text}</p>
            ) : null}

            <section className="catchup-section">
              <h4>Top Priorities</h4>
              {(payload.top_priorities || []).length === 0 ? (
                <p className="briefing-empty-line">No urgent items in this window.</p>
              ) : (
                <ul className="catchup-list">
                  {(payload.top_priorities || []).map((item, index) => (
                    <li key={`${item.source_email_id || item.title}-${index}`}>
                      <button
                        type="button"
                        className="catchup-item"
                        disabled={!item.source_email_id}
                        onClick={() =>
                          item.source_email_id &&
                          onOpenEmail(item.source_email_id)
                        }
                      >
                        <strong>{item.title || item.subject}</strong>
                        {item.reason ? <span>{item.reason}</span> : null}
                        {item.sender ? (
                          <em>From {item.sender}</em>
                        ) : null}
                      </button>
                    </li>
                  ))}
                </ul>
              )}
            </section>

            <section className="catchup-section">
              <h4>Important Updates</h4>
              {(payload.important_updates || []).length === 0 ? (
                <p className="briefing-empty-line">No notable updates.</p>
              ) : (
                <ul className="catchup-bullets">
                  {(payload.important_updates || []).map((line, index) => (
                    <li key={`${index}-${line.slice(0, 24)}`}>{line}</li>
                  ))}
                </ul>
              )}
            </section>

            <section className="catchup-section">
              <h4>Upcoming Deadlines</h4>
              {(payload.deadlines || []).length === 0 ? (
                <p className="briefing-empty-line">No deadlines found.</p>
              ) : (
                <ul className="catchup-list">
                  {(payload.deadlines || []).map((item, index) => (
                    <li key={`${item.source_email_id}-${item.due_date}-${index}`}>
                      <button
                        type="button"
                        className="catchup-item"
                        disabled={!item.source_email_id}
                        onClick={() =>
                          item.source_email_id &&
                          onOpenEmail(item.source_email_id)
                        }
                      >
                        <strong>
                          {item.due_date ? `${item.due_date} · ` : ""}
                          {item.title}
                        </strong>
                      </button>
                    </li>
                  ))}
                </ul>
              )}
            </section>

            <section className="catchup-section">
              <h4>Follow-ups</h4>
              {(payload.followups?.outgoing_awaiting_recipient || []).length ===
                0 &&
              (payload.followups?.incoming_awaiting_user || []).length === 0 ? (
                <p className="briefing-empty-line">No outstanding follow-ups.</p>
              ) : (
                <ul className="catchup-list">
                  {(payload.followups?.outgoing_awaiting_recipient || []).map(
                    (row, index) => (
                      <li
                        key={`out-${row.recommendation_key || row.source_email_id}-${index}`}
                      >
                        <div className="catchup-item static">
                          <strong>
                            Waiting on them: {row.title || row.subject}
                          </strong>
                          <span>
                            {row.recipient || "Recipient"}
                            {row.waiting_hours
                              ? ` · ${row.waiting_hours}h`
                              : ""}
                          </span>
                          {row.actions_enabled && row.recommendation_key ? (
                            <div className="briefing-followup-actions">
                              <button
                                type="button"
                                className="briefing-followup-btn"
                                disabled={Boolean(busyKey)}
                                onClick={() =>
                                  void runFollowupAction(
                                    "draft",
                                    row.recommendation_key,
                                    row.source_email_id
                                  )
                                }
                              >
                                Draft Follow-up
                              </button>
                              <button
                                type="button"
                                className="briefing-followup-btn"
                                disabled={Boolean(busyKey)}
                                onClick={() =>
                                  void runFollowupAction(
                                    "remind",
                                    row.recommendation_key
                                  )
                                }
                              >
                                Remind Tomorrow
                              </button>
                              <button
                                type="button"
                                className="briefing-followup-btn muted"
                                disabled={Boolean(busyKey)}
                                onClick={() =>
                                  void runFollowupAction(
                                    "dismiss",
                                    row.recommendation_key
                                  )
                                }
                              >
                                Dismiss
                              </button>
                            </div>
                          ) : null}
                        </div>
                      </li>
                    )
                  )}
                  {(payload.followups?.incoming_awaiting_user || []).map(
                    (row, index) => (
                      <li
                        key={`in-${row.recommendation_key || row.source_email_id}-${index}`}
                      >
                        <div className="catchup-item static">
                          <strong>
                            Needs your reply: {row.title || row.subject}
                          </strong>
                          <span>From {row.sender || "sender"}</span>
                          {row.source_email_id ? (
                            <div className="briefing-followup-actions">
                              <button
                                type="button"
                                className="briefing-followup-btn"
                                onClick={() =>
                                  onOpenEmail(row.source_email_id!, {
                                    tab: "draft",
                                  })
                                }
                              >
                                Draft Reply
                              </button>
                              <button
                                type="button"
                                className="briefing-followup-btn"
                                onClick={() =>
                                  onOpenEmail(row.source_email_id!)
                                }
                              >
                                View Email
                              </button>
                            </div>
                          ) : null}
                        </div>
                      </li>
                    )
                  )}
                </ul>
              )}
            </section>
          </>
        ) : null}
      </div>
    </div>
  );
}
