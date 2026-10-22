import { useCallback, useEffect, useState } from "react";
import type { BriefingVisibleRange } from "../BriefingPanel";
import { API } from "../api";

type HistoryDay = {
  date: string;
};

type HistoryResponse = {
  timezone: string;
  today: string;
  days: HistoryDay[];
};

/**
 * Loads briefing history solely to derive the visible date strip range
 * (for inbox filtering) without mounting BriefingPanel.
 */
export function useBriefingVisibleRange() {
  const [visibleRange, setVisibleRange] = useState<BriefingVisibleRange | null>(
    null
  );
  const [rangeReady, setRangeReady] = useState(false);

  const refresh = useCallback(async () => {
    try {
      const response = await fetch(`${API}/db/briefings/history`);
      const data = (await response.json().catch(() => null)) as
        | HistoryResponse
        | null;
      if (!response.ok || !data || !Array.isArray(data.days) || !data.days.length) {
        setVisibleRange(null);
        return;
      }
      const startDate = data.days[0]?.date;
      const endDate = data.days[data.days.length - 1]?.date;
      if (startDate && endDate) {
        setVisibleRange({
          startDate,
          endDate,
          timezone: data.timezone || "",
        });
      } else {
        setVisibleRange(null);
      }
    } catch {
      setVisibleRange(null);
    } finally {
      setRangeReady(true);
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  return { visibleRange, rangeReady, refreshRange: refresh };
}
