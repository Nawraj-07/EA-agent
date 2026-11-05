import { useCallback, useEffect, useState } from "react";
import { API } from "../api";
import type { BriefingContent } from "../home/briefingTypes";

export type { BriefingContent };

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

type BriefingHistorySnapshot = {
  history: BriefingHistory;
  homeSelectedDate: string;
};

let briefingHistorySnapshot: BriefingHistorySnapshot | null = null;

export function readBriefingHistoryCache(): BriefingHistorySnapshot | null {
  return briefingHistorySnapshot;
}

export function writeBriefingHistoryCache(
  history: BriefingHistory,
  homeSelectedDate?: string
) {
  const previous = briefingHistorySnapshot?.homeSelectedDate ?? "";
  const kept =
    homeSelectedDate ??
    (previous && history.days.some((day) => day.date === previous)
      ? previous
      : "");
  briefingHistorySnapshot = {
    history,
    homeSelectedDate: kept,
  };
}

export function rememberBriefingHomeDate(date: string) {
  if (!briefingHistorySnapshot) return;
  briefingHistorySnapshot = {
    ...briefingHistorySnapshot,
    homeSelectedDate: date,
  };
}

export function useBriefingHistory() {
  const cached = readBriefingHistoryCache();
  const [history, setHistory] = useState<BriefingHistory | null>(
    () => cached?.history ?? null
  );
  const [selectedDate, setSelectedDateState] = useState(
    () => cached?.homeSelectedDate || cached?.history.today || ""
  );
  const [loading, setLoading] = useState(() => readBriefingHistoryCache() == null);
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
      const nextHistory = {
        timezone: payload.timezone || "",
        today: payload.today || "",
        days,
      };
      setHistory(nextHistory);
      const preferred =
        preferDate && days.some((d) => d.date === preferDate)
          ? preferDate
          : payload.today && days.some((d) => d.date === payload.today)
            ? payload.today
            : days[days.length - 1]?.date || "";
      setSelectedDateState(preferred);
      writeBriefingHistoryCache(nextHistory, preferred);
    } catch (err) {
      setHistory(null);
      setError(err instanceof Error ? err.message : "Something went wrong");
    } finally {
      setLoading(false);
    }
  }, []);

  const setSelectedDate = useCallback((date: string) => {
    setSelectedDateState(date);
    rememberBriefingHomeDate(date);
  }, []);

  useEffect(() => {
    if (readBriefingHistoryCache()) return;
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
    rangeReady: visibleRange !== null || (!loading && error !== ""),
    loading,
    error,
    reload: load,
  };
}
