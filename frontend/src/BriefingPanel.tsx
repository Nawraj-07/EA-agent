import { useCallback, useEffect, useState } from "react";

const API = import.meta.env.VITE_API_URL || "http://localhost:8000";

type BriefingContent = {
  headline?: string;
  highlights?: string[];
  deadlines?: string[];
  action_items?: string[];
  notes?: string;
  email_count?: number;
};

type Briefing = {
  id: string;
  content: BriefingContent;
  email_count: number;
  period_start?: string | null;
  period_end?: string | null;
  created_at?: string | null;
};

type LatestBriefingResponse = {
  briefing: Briefing | null;
  status?: "ok" | "empty";
  message?: string;
};

function extractDetail(data: unknown): string | null {
  if (!data || typeof data !== "object") return null;
  const detail = (data as { detail?: unknown }).detail;
  if (typeof detail === "string") return detail;
  return null;
}

export default function BriefingPanel() {
  const [briefing, setBriefing] = useState<Briefing | null>(null);
  const [emptyMessage, setEmptyMessage] = useState("");
  const [loading, setLoading] = useState(true);
  const [generating, setGenerating] = useState(false);
  const [error, setError] = useState("");

  const loadBriefing = useCallback(async () => {
    try {
      setLoading(true);
      setError("");

      const response = await fetch(`${API}/db/briefings/latest`);
      const data = (await response.json().catch(() => null)) as
        | LatestBriefingResponse
        | { detail?: string }
        | null;

      if (!response.ok) {
        throw new Error(
          extractDetail(data) ||
            `Failed to load briefing (${response.status})`
        );
      }

      const payload = data as LatestBriefingResponse | null;
      const next = payload?.briefing ?? null;
      setBriefing(next);
      setEmptyMessage(
        next
          ? ""
          : payload?.message ||
              "No briefing has been generated yet."
      );
    } catch (err) {
      setBriefing(null);
      setEmptyMessage("");
      setError(err instanceof Error ? err.message : "Something went wrong");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void loadBriefing();
  }, [loadBriefing]);

  async function generateBriefing() {
    if (generating) return;

    setGenerating(true);
    setError("");

    try {
      const response = await fetch(`${API}/internal/briefing-now`, {
        method: "POST",
      });
      const data = await response.json().catch(() => null);

      if (!response.ok) {
        throw new Error(
          extractDetail(data) ||
            `Briefing generation failed (${response.status})`
        );
      }

      await loadBriefing();
    } catch (err) {
      setError(
        err instanceof Error ? err.message : "Briefing generation failed"
      );
    } finally {
      setGenerating(false);
    }
  }

  const content = briefing?.content ?? {};
  const highlights = content.highlights ?? [];
  const deadlines = content.deadlines ?? [];
  const actionItems = content.action_items ?? [];

  return (
    <section className="briefing-panel">
      <div className="briefing-header">
        <h2>Daily briefing</h2>
        <div className="briefing-actions">
          <button
            type="button"
            className="inbox-refresh-btn"
            onClick={() => void loadBriefing()}
            disabled={loading || generating}
          >
            {loading ? "Loading…" : "Refresh"}
          </button>
          <button
            type="button"
            className="inbox-refresh-btn"
            onClick={() => void generateBriefing()}
            disabled={loading || generating}
          >
            {generating ? "Generating…" : "Generate briefing"}
          </button>
        </div>
      </div>

      {error && (
        <p className="inbox-error" role="alert">
          {error}
        </p>
      )}

      {loading && !briefing ? (
        <p className="inbox-message">Loading briefing…</p>
      ) : !briefing ? (
        <div className="inbox-message">
          <p>{emptyMessage || "No briefing yet."}</p>
          <p className="briefing-hint">
            Click <strong>Generate briefing</strong> (requires{" "}
            <code>ENABLE_MANUAL_SYNC=true</code>), or wait for the daily
            scheduler job.
          </p>
        </div>
      ) : (
        <div className="briefing-body">
          <h3>{content.headline || "Daily briefing"}</h3>
          {briefing.created_at && (
            <p className="briefing-meta">
              Generated {new Date(briefing.created_at).toLocaleString()} ·{" "}
              {briefing.email_count ?? content.email_count ?? 0} emails in
              window
            </p>
          )}

          {content.notes && <p className="briefing-notes">{content.notes}</p>}

          <div className="briefing-columns">
            <div>
              <h4>Highlights</h4>
              {highlights.length ? (
                <ul>
                  {highlights.map((item, index) => (
                    <li key={`h-${index}`}>{item}</li>
                  ))}
                </ul>
              ) : (
                <p>None</p>
              )}
            </div>
            <div>
              <h4>Deadlines</h4>
              {deadlines.length ? (
                <ul>
                  {deadlines.map((item, index) => (
                    <li key={`d-${index}`}>{item}</li>
                  ))}
                </ul>
              ) : (
                <p>None</p>
              )}
            </div>
            <div>
              <h4>Action items</h4>
              {actionItems.length ? (
                <ul>
                  {actionItems.map((item, index) => (
                    <li key={`a-${index}`}>{item}</li>
                  ))}
                </ul>
              ) : (
                <p>None</p>
              )}
            </div>
          </div>
        </div>
      )}
    </section>
  );
}
