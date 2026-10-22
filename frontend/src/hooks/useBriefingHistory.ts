import { useCallback, useEffect, useState } from "react";
import { API } from "../api";

export type BriefingContent = {
  headline?: string;
  highlights?: string[];
  deadlines?: string[];
  action_items?: string[];
  notes?: string;
};

export type Briefing = {
  id: string;
  content: BriefingContent;
  email_count?: number;
  created_at?: string | null;
};

export type HistoryDay = {
  date: string;
  has_briefing: boolean;
  briefing: Briefing | null;
};

export type BriefingHistory = {
  timezone: string;
  today: string;
  days: HistoryDay[];
};

export function useBriefingHistory() {
  const [history, setHistory] = useState<BriefingHistory | null>(null);
  const [selectedDate, setSelectedDate] = useState("");
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");

  const load = useCallback(async (preferDate?: string) => {
    try {
      setLoading(true);
      setError("");
      const response = await fetch(`${API}/db/briefings/history`);
      const data = (await response.json().catch(() => null)) as
        | BriefingHistory
        | { detail?: string }
        | null;
      if (!response.ok) {
        const detail =
          data && typeof data === "object" && "detail" in data
            ? String((data as { detail: unknown }).detail)
            : null;
        throw new Error(
          detail || `Failed to load briefing history (${response.status})`
        );
      }
      const payload = data as BriefingHistory;
      const days = Array.isArray(payload.days) ? payload.days : [];
      setHistory({
        timezone: payload.timezone || "",
        today: payload.today || "",
        days,
      });
      const preferred =
        preferDate && days.some((d) => d.date === preferDate)
          ? preferDate
          : payload.today && days.some((d) => d.date === payload.today)
            ? payload.today
            : days[days.length - 1]?.date || "";
      setSelectedDate(preferred);
    } catch (err) {
      setHistory(null);
      setError(err instanceof Error ? err.message : "Something went wrong");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const visibleRange =
    history && history.days.length
      ? {
          startDate: history.days[0].date,
          endDate: history.days[history.days.length - 1].date,
          timezone: history.timezone,
        }
      : null;

  const selectedDay =
    history?.days.find((d) => d.date === selectedDate) ?? null;

  return {
    history,
    selectedDate,
    setSelectedDate,
    selectedDay,
    visibleRange,
    rangeReady: !loading && (visibleRange !== null || error !== ""),
    loading,
    error,
    reload: load,
  };
}
