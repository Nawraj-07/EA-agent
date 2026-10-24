import { useEffect, useRef, useState } from "react";
import { API } from "./api";

type Draft = {
  id: string;
  email_id: string;
  body: string;
  status: string;
  gmail_sent_message_id?: string | null;
  recipient?: string | null;
};

type DraftPanelProps = {
  emailId: string;
  sender?: string | null;
  embedded?: boolean;
  autoGenerateToken?: number;
};

function isNoReplyBody(body: string | undefined | null): boolean {
  if (!body) return false;
  const normalized = body.trim().replace(/\.$/, "");
  return normalized === "NO_REPLY_NEEDED";
}

function extractDetail(data: unknown): string | null {
  if (!data || typeof data !== "object") return null;

  const detail = (data as { detail?: unknown }).detail;

  if (typeof detail === "string") return detail;
  if (Array.isArray(detail)) {
    return detail
      .map((item) =>
        typeof item === "object" && item && "msg" in item
          ? String((item as { msg: unknown }).msg)
          : String(item)
      )
      .join(" ");
  }

  return null;
}

function extractEmailAddress(raw: string | null | undefined): string | null {
  if (!raw || !raw.trim()) return null;

  const angled = raw.match(/<([^>]+)>/);
  const candidate = (angled?.[1] || raw).trim().replace(/^<|>$/g, "");

  if (/^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(candidate)) {
    return candidate.toLowerCase();
  }

  return null;
}

function formatStatus(status: string | undefined): string {
  const value = (status || "pending").toLowerCase();

  if (value === "sent") return "SENT";
  if (value === "sending") return "SENDING";
  if (value === "approved") return "APPROVED";
  if (value === "rejected") return "REJECTED";
  return "PENDING";
}

export default function DraftPanel({
  emailId,
  sender,
  embedded = false,
  autoGenerateToken = 0,
}: DraftPanelProps) {
  const [draft, setDraft] = useState<Draft | null>(null);
  const [draftBody, setDraftBody] = useState("");
  const [loading, setLoading] = useState(false);
  const [actionLoading, setActionLoading] = useState(false);
  const [sending, setSending] = useState(false);
  const [error, setError] = useState("");
  const [info, setInfo] = useState("");
  const [success, setSuccess] = useState("");
  const [showConfirm, setShowConfirm] = useState(false);

  const statusValue = draft?.status?.toLowerCase() || "";
  const isPending = statusValue === "pending";
  const isSent = statusValue === "sent";
  const busy = loading || actionLoading || sending;
  const actionsDisabled = !draft || !isPending || busy;

  const recipientDisplay =
    draft?.recipient ||
    extractEmailAddress(sender) ||
    (sender?.trim() ? sender.trim() : null);

  async function generateDraft() {
    if (busy) return;

    setLoading(true);
    setError("");
    setInfo("");
    setSuccess("");
    setShowConfirm(false);
    setDraft(null);
    setDraftBody("");

    try {
      const response = await fetch(`${API}/emails/${emailId}/draft`, {
        method: "POST",
      });

      const data = await response.json().catch(() => null);

      if (response.status === 422) {
        setInfo(
          extractDetail(data) || "This email does not require a reply."
        );
        return;
      }

      if (!response.ok) {
        throw new Error(
          extractDetail(data) ||
            `Draft generation failed (${response.status})`
        );
      }

      const result = data as Draft | null;

      if (!result || isNoReplyBody(result.body)) {
        setInfo("This email does not require a reply.");
        return;
      }

      setDraft(result);
      setDraftBody(result.body);
    } catch (err) {
      setError(
        err instanceof Error ? err.message : "Something went wrong"
      );
    } finally {
      setLoading(false);
    }
  }

  const generateDraftRef = useRef(generateDraft);
  generateDraftRef.current = generateDraft;
  const draftRef = useRef(draft);
  draftRef.current = draft;
  const busyRef = useRef(busy);
  busyRef.current = busy;

  useEffect(() => {
    if (!autoGenerateToken) return;
    if (draftRef.current || busyRef.current) return;
    void generateDraftRef.current();
  }, [autoGenerateToken]);

  async function patchDraft(payload: { body?: string; status?: string }) {
    if (!draft || busy) return;

    setActionLoading(true);
    setError("");
    setInfo("");
    setSuccess("");

    try {
      const response = await fetch(`${API}/drafts/${draft.id}`, {
        method: "PATCH",
        headers: {
          "Content-Type": "application/json",
        },
        body: JSON.stringify(payload),
      });

      const data = await response.json().catch(() => null);

      if (!response.ok) {
        throw new Error(
          extractDetail(data) || `Draft update failed (${response.status})`
        );
      }

      const updated = data as Draft;
      setDraft(updated);
      setDraftBody(updated.body);
    } catch (err) {
      setError(
        err instanceof Error ? err.message : "Something went wrong"
      );
    } finally {
      setActionLoading(false);
    }
  }

  async function saveEdit() {
    if (!draftBody.trim()) {
      setError("Draft body cannot be empty.");
      return;
    }

    await patchDraft({ body: draftBody });
  }

  async function rejectDraft() {
    await patchDraft({ status: "rejected" });
  }

  function openApproveConfirm() {
    if (actionsDisabled) return;

    const recipient = extractEmailAddress(sender) || recipientDisplay;
    if (!recipient) {
      setError(
        "Cannot send: the original sender does not contain a valid email address."
      );
      return;
    }

    setError("");
    setShowConfirm(true);
  }

  function cancelApproveConfirm() {
    if (sending) return;
    setShowConfirm(false);
  }

  async function confirmApproveAndSend() {
    if (!draft || sending || actionLoading || loading) return;

    setSending(true);
    setError("");
    setInfo("");
    setSuccess("");

    try {
      const response = await fetch(`${API}/drafts/${draft.id}/approve`, {
        method: "POST",
      });

      const data = await response.json().catch(() => null);

      if (!response.ok) {
        throw new Error(
          extractDetail(data) ||
            `Approve and send failed (${response.status})`
        );
      }

      const updated = data as Draft;
      setDraft(updated);
      setDraftBody(updated.body);
      setShowConfirm(false);

      const sentTo = updated.recipient || recipientDisplay || "the recipient";
      setSuccess(
        `Reply sent successfully to ${sentTo}.` +
          (updated.gmail_sent_message_id
            ? ` Gmail message id: ${updated.gmail_sent_message_id}.`
            : "")
      );
    } catch (err) {
      setShowConfirm(false);
      setError(
        err instanceof Error
          ? err.message
          : "Something went wrong while sending."
      );
      // Do not auto-retry — especially for uncertain Gmail outcomes.
    } finally {
      setSending(false);
    }
  }

  const statusLabel = formatStatus(draft?.status);
  const statusClass = statusLabel.toLowerCase();

  return (
    <section className={`draft-panel${embedded ? " embedded" : ""}`}>
      {embedded ? null : <h3>AI Draft Reply</h3>}

      <button
        type="button"
        className="draft-generate-btn"
        onClick={generateDraft}
        disabled={busy}
      >
        {loading ? "Generating draft..." : "Generate Draft"}
      </button>

      {loading && (
        <p className="draft-loading">Generating draft...</p>
      )}

      {actionLoading && !sending && (
        <p className="draft-loading">Updating draft...</p>
      )}

      {sending && (
        <p className="draft-loading">Sending reply via Gmail...</p>
      )}

      {info && !error && !success && (
        <p className="draft-info">{info}</p>
      )}

      {success && (
        <p className="draft-success" role="status">
          {success}
        </p>
      )}

      {error && (
        <p className="draft-error" role="alert">
          {error}
        </p>
      )}

      {draft && (
        <div className="draft-result">
          <span className={`draft-status ${statusClass}`}>
            {statusLabel}
          </span>

          <textarea
            className="draft-body-input"
            value={draftBody}
            onChange={(event) => setDraftBody(event.target.value)}
            disabled={!isPending || busy}
            rows={10}
            aria-label="Draft reply"
          />

          <div className="draft-actions">
            <button
              type="button"
              onClick={saveEdit}
              disabled={actionsDisabled || !draftBody.trim()}
            >
              {actionLoading && !sending ? "Saving..." : "Save Edit"}
            </button>

            <button
              type="button"
              className="draft-approve-btn"
              onClick={openApproveConfirm}
              disabled={actionsDisabled}
            >
              {sending ? "Sending..." : "Approve & Send"}
            </button>

            <button
              type="button"
              className="draft-reject-btn"
              onClick={rejectDraft}
              disabled={actionsDisabled}
            >
              Reject
            </button>
          </div>

          {isSent && !success && (
            <p className="draft-success">
              This draft has already been sent
              {draft.recipient ? ` to ${draft.recipient}` : ""}.
            </p>
          )}
        </div>
      )}

      {showConfirm && (
        <div
          className="draft-confirm-overlay"
          role="presentation"
          onClick={cancelApproveConfirm}
        >
          <div
            className="draft-confirm-dialog"
            role="alertdialog"
            aria-modal="true"
            aria-labelledby="draft-confirm-title"
            aria-describedby="draft-confirm-desc"
            onClick={(event) => event.stopPropagation()}
          >
            <h4 id="draft-confirm-title">Send this reply?</h4>
            <p id="draft-confirm-desc">
              Confirming will send a <strong>real Gmail reply</strong> to:
            </p>
            <p className="draft-confirm-recipient">
              {recipientDisplay || "Unknown recipient"}
            </p>
            <p className="draft-confirm-note">
              This cannot be undone from this app. Double-check the recipient
              before continuing.
            </p>
            <div className="draft-confirm-actions">
              <button
                type="button"
                className="draft-confirm-cancel"
                onClick={cancelApproveConfirm}
                disabled={sending}
              >
                Cancel
              </button>
              <button
                type="button"
                className="draft-approve-btn"
                onClick={confirmApproveAndSend}
                disabled={sending}
              >
                {sending ? "Sending..." : "Confirm & Send"}
              </button>
            </div>
          </div>
        </div>
      )}
    </section>
  );
}
