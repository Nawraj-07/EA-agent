import { useEffect, useMemo, useState, type FormEvent } from "react";
import { Link, useNavigate } from "react-router-dom";
import { API } from "../api";

type BriefingContent = {
  headline?: string;
  highlights?: string[];
  deadlines?: string[];
  action_items?: string[];
  notes?: string;
};

type Briefing = {
  id: string;
  content: BriefingContent;
  email_count?: number;
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

function greetingForHour(hour: number): string {
  if (hour < 12) return "Good morning";
  if (hour < 17) return "Good afternoon";
  return "Good evening";
}

function pad2(n: number): string {
  return n.toString().padStart(2, "0");
}

export default function OverviewPage() {
  const navigate = useNavigate();
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [todayBriefing, setTodayBriefing] = useState<Briefing | null>(null);
  const [timezone, setTimezone] = useState("");
  const [question, setQuestion] = useState("");

  const greeting = useMemo(
    () => greetingForHour(new Date().getHours()),
    []
  );

  useEffect(() => {
    let cancelled = false;
    async function load() {
      try {
        setLoading(true);
        setError("");
        const response = await fetch(`${API}/db/briefings/history`);
        const data = (await response.json().catch(() => null)) as
          | HistoryResponse
          | { detail?: string }
          | null;
        if (!response.ok) {
          const detail =
            data && typeof data === "object" && "detail" in data
              ? String((data as { detail: unknown }).detail)
              : null;
          throw new Error(
            detail || `Failed to load briefing (${response.status})`
          );
        }
        const payload = data as HistoryResponse;
        if (cancelled) return;
        setTimezone(payload.timezone || "");
        const todaySlot =
          payload.days?.find((d) => d.date === payload.today) ??
          payload.days?.[payload.days.length - 1] ??
          null;
        setTodayBriefing(todaySlot?.briefing ?? null);
      } catch (err) {
        if (!cancelled) {
          setTodayBriefing(null);
          setError(err instanceof Error ? err.message : "Something went wrong");
        }
      } finally {
        if (!cancelled) setLoading(false);
      }
    }
    void load();
    return () => {
      cancelled = true;
    };
  }, []);

  const content = todayBriefing?.content ?? {};
  const actionItems = content.action_items ?? [];
  const deadlines = content.deadlines ?? [];
  const highlights = content.highlights ?? [];
  const followUps = actionItems.filter((item) =>
    /follow|reply|respond|pending/i.test(item)
  );

  function onAsk(event: FormEvent) {
    event.preventDefault();
    const q = question.trim();
    navigate("/assistant", { state: q ? { question: q } : undefined });
  }

  return (
    <div className="ea-overview ea-panel-page">
      <div className="ea-overview-greeting">
        <h1>{greeting}</h1>
        <p>Here&apos;s what needs your attention today.</p>
      </div>

      {error && <p className="inbox-error">{error}</p>}

      <div className="ea-stat-row">
        <div className="ea-stat-card">
          <strong>{pad2(actionItems.length)}</strong>
          <span>Needs action</span>
        </div>
        <div className="ea-stat-card">
          <strong>{pad2(followUps.length)}</strong>
          <span>Follow-ups</span>
        </div>
        <div className="ea-stat-card">
          <strong>{pad2(deadlines.length)}</strong>
          <span>Deadline</span>
        </div>
      </div>

      <section className="ea-overview-card">
        <div className="ea-overview-card-head">
          <div>
            <h2>Daily Briefing</h2>
            <p>
              {content.headline ||
                (loading
                  ? "Loading priorities…"
                  : "Your priorities, deadlines and action items")}
              {timezone ? ` · ${timezone}` : ""}
            </p>
          </div>
          <Link className="ea-overview-link" to="/briefings" title="Open briefings">
            ↗
          </Link>
        </div>

        {loading ? (
          <p className="ea-muted">Loading briefing…</p>
        ) : actionItems.length === 0 && highlights.length === 0 ? (
          <p className="ea-muted">
            No briefing for today yet. Open Briefings to generate one.
          </p>
        ) : (
          <ul className="ea-agenda-list">
            {(actionItems.length ? actionItems : highlights)
              .slice(0, 6)
              .map((item) => (
                <li key={item}>
                  <span className="ea-agenda-dot" aria-hidden />
                  <span>{item}</span>
                </li>
              ))}
          </ul>
        )}
      </section>

      <section className="ea-ask-block">
        <div className="ea-ask-block-title">
          <span className="ea-brand-mark" aria-hidden>
            ✦
          </span>
          Ask your inbox
        </div>
        <p>What important emails need my attention?</p>
        <form className="ea-ask-form" onSubmit={onAsk}>
          <input
            className="ea-ask-input"
            type="text"
            value={question}
            onChange={(e) => setQuestion(e.target.value)}
            placeholder="Ask anything about your emails..."
            aria-label="Ask your inbox"
          />
          <button className="ea-ask-submit" type="submit">
            Ask
          </button>
        </form>
      </section>
    </div>
  );
}
