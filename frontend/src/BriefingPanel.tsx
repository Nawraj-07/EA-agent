import { useCallback, useEffect, useMemo, useState } from "react";

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
  gmail_account_email?: string;
};

type HistoryDay = {
  date: string;
  has_briefing: boolean;
  briefing: Briefing | null;
};

type HistoryResponse = {
  timezone: string;
  today: string;
  days: HistoryDay[];
};

function extractDetail(data: unknown): string | null {
  if (!data || typeof data !== "object") return null;
  const detail = (data as { detail?: unknown }).detail;
  if (typeof detail === "string") return detail;
  return null;
}

function formatDayLabel(isoDate: string): { weekday: string; day: string } {
  const parsed = new Date(`${isoDate}T12:00:00`);
  return {
    weekday: parsed.toLocaleDateString(undefined, { weekday: "short" }),
    day: parsed.toLocaleDateString(undefined, { day: "numeric" }),
  };
}

export type BriefingVisibleRange = {
  startDate: string;
  endDate: string;
  timezone: string;
};

type BriefingPanelProps = {
  onVisibleRangeChange?: (range: BriefingVisibleRange | null) => void;
};

export default function BriefingPanel({
  onVisibleRangeChange,
}: BriefingPanelProps) {
  const [days, setDays] = useState<HistoryDay[]>([]);
  const [today, setToday] = useState("");
  const [timezone, setTimezone] = useState("");
  const [selectedDate, setSelectedDate] = useState("");
  const [loading, setLoading] = useState(true);
  const [generating, setGenerating] = useState(false);
  const [error, setError] = useState("");

  const loadHistory = useCallback(async (preferDate?: string) => {
    try {
      setLoading(true);
      setError("");

      const response = await fetch(`${API}/db/briefings/history`);
      const data = (await response.json().catch(() => null)) as
        | HistoryResponse
        | { detail?: string }
        | null;

      if (!response.ok) {
        const detail = extractDetail(data);
        if (response.status === 404) {
          throw new Error(
            "Briefing history API not found. Restart the backend with: uvicorn main:app --reload --port 8000"
          );
        }
        throw new Error(
          detail || `Failed to load briefing history (${response.status})`
        );
      }

      const payload = data as HistoryResponse;
      const nextDays = Array.isArray(payload.days) ? payload.days : [];
      setDays(nextDays);
      setToday(payload.today || "");
      setTimezone(payload.timezone || "");

      const preferred =
        preferDate && nextDays.some((d) => d.date === preferDate)
          ? preferDate
          : payload.today && nextDays.some((d) => d.date === payload.today)
            ? payload.today
            : nextDays[nextDays.length - 1]?.date || "";

      setSelectedDate(preferred);

      const startDate = nextDays[0]?.date;
      const endDate = nextDays[nextDays.length - 1]?.date;
      if (startDate && endDate) {
        onVisibleRangeChange?.({
          startDate,
          endDate,
          timezone: payload.timezone || "",
        });
      } else {
        onVisibleRangeChange?.(null);
      }
    } catch (err) {
      setDays([]);
      setError(err instanceof Error ? err.message : "Something went wrong");
      onVisibleRangeChange?.(null);
    } finally {
      setLoading(false);
    }
  }, [onVisibleRangeChange]);

  useEffect(() => {
    void loadHistory();
  }, [loadHistory]);

  async function generateBriefing() {
    if (generating) return;

    const dateForGenerate = selectedDate || today;
    if (!dateForGenerate) {
      setError("Select a date before generating a briefing.");
      return;
    }

    setGenerating(true);
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

      await loadHistory(dateForGenerate);
    } catch (err) {
      setError(
        err instanceof Error ? err.message : "Briefing generation failed"
      );
    } finally {
      setGenerating(false);
    }
  }

  const selectedDay = useMemo(
    () => days.find((d) => d.date === selectedDate) ?? null,
    [days, selectedDate]
  );

  const briefing = selectedDay?.briefing ?? null;
  const content = briefing?.content ?? {};
  const highlights = content.highlights ?? [];
  const deadlines = content.deadlines ?? [];
  const actionItems = content.action_items ?? [];

  return (
    <section className="briefing-panel">
      <div className="briefing-header">
        <div>
          <h2>Daily briefing</h2>
          {timezone && (
            <p className="briefing-timezone">Timezone: {timezone}</p>
          )}
        </div>
        <div className="briefing-actions">
          <button
            type="button"
            className="inbox-refresh-btn"
            onClick={() => void loadHistory(selectedDate || undefined)}
            disabled={loading || generating}
          >
            {loading ? "Loading…" : "Refresh"}
          </button>
          <button
            type="button"
            className="inbox-refresh-btn"
            onClick={() => void generateBriefing()}
            disabled={loading || generating || !selectedDate}
          >
            {generating
              ? "Generating…"
              : selectedDay?.has_briefing
                ? "Regenerate for this day"
                : "Generate briefing"}
          </button>
        </div>
      </div>

      {error && (
        <p className="inbox-error" role="alert">
          {error}
        </p>
      )}

      {!loading && days.length > 0 && (
        <div className="briefing-date-strip" role="tablist" aria-label="Briefing dates">
          {days.map((day) => {
            const label = formatDayLabel(day.date);
            const isSelected = day.date === selectedDate;
            const isToday = day.date === today;
            return (
              <button
                key={day.date}
                type="button"
                role="tab"
                aria-selected={isSelected}
                className={[
                  "briefing-date-chip",
                  isSelected ? "active" : "",
                  day.has_briefing ? "has-briefing" : "empty-day",
                  isToday ? "is-today" : "",
                ]
                  .filter(Boolean)
                  .join(" ")}
                onClick={() => setSelectedDate(day.date)}
              >
                <span className="briefing-date-weekday">{label.weekday}</span>
                <span className="briefing-date-day">{label.day}</span>
              </button>
            );
          })}
        </div>
      )}

      {loading && !briefing ? (
        <p className="inbox-message">Loading briefing…</p>
      ) : !selectedDay ? (
        <div className="inbox-message">
          <p>No briefing history available yet.</p>
          <p className="briefing-hint">
            Click <strong>Generate briefing</strong> (requires{" "}
            <code>ENABLE_MANUAL_SYNC=true</code>), or wait for the daily
            scheduler job.
          </p>
        </div>
      ) : !selectedDay.has_briefing || !briefing ? (
        <div className="inbox-message">
          <p>No briefing for this day.</p>
          {selectedDate === today && (
            <p className="briefing-hint">
              Generate one for today, or wait for the scheduled job.
            </p>
          )}
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
