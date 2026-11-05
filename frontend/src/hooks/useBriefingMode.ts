import { useCallback, useEffect, useMemo, useState } from "react";
import { API } from "../api";
import type { BriefingContent } from "../home/briefingTypes";
import {
  type BriefingMode,
  isMondayInTimezone,
  resolveBriefingTimezone,
} from "../home/briefingModeUtils";

export type { BriefingMode };

type ModeLatestResponse = {
  mode?: string;
  status?: string;
  briefing?: {
    id?: string;
    content?: BriefingContent;
    email_count?: number;
    period_key?: string;
    created_at?: string;
    status?: string;
  } | null;
  detail?: string;
};

const MODE_POLL_MS = 60_000;

function extractDetail(data: unknown): string | null {
  if (!data || typeof data !== "object") return null;
  const detail = (data as { detail?: unknown }).detail;
  return typeof detail === "string" ? detail : null;
}

/**
 * Home Page smart briefing mode selection + Monday visibility + mode fetch.
 * Filter switches only GET saved data — never OpenAI generate.
 */
export function useBriefingMode(configuredTimezone?: string | null) {
  const timezone = resolveBriefingTimezone(configuredTimezone);
  const [mode, setModeState] = useState<BriefingMode>("morning");
  const [mondayVisible, setMondayVisible] = useState(() =>
    isMondayInTimezone(timezone)
  );
  const [modeContent, setModeContent] = useState<BriefingContent | null>(null);
  const [modeLoading, setModeLoading] = useState(false);
  const [modeError, setModeError] = useState("");
  const [modeStatus, setModeStatus] = useState<string>("");
  const [modeHasBriefing, setModeHasBriefing] = useState(false);

  const refreshMondayVisibility = useCallback(() => {
    const visible = isMondayInTimezone(timezone);
    setMondayVisible(visible);
    return visible;
  }, [timezone]);

  const setMode = useCallback(
    (next: BriefingMode) => {
      if (next === "monday" && !isMondayInTimezone(timezone)) {
        setModeState("morning");
        return;
      }
      setModeState(next);
    },
    [timezone]
  );

  // Refresh Monday chip when TZ day changes without full page reload.
  useEffect(() => {
    const tick = () => {
      const visible = refreshMondayVisibility();
      setModeState((current) => {
        if (current === "monday" && !visible) return "morning";
        return current;
      });
    };
    tick();
    const timer = window.setInterval(tick, MODE_POLL_MS);
    const onVis = () => {
      if (document.visibilityState === "visible") tick();
    };
    document.addEventListener("visibilitychange", onVis);
    return () => {
      window.clearInterval(timer);
      document.removeEventListener("visibilitychange", onVis);
    };
  }, [refreshMondayVisibility]);

  const loadModeLatest = useCallback(async (target: BriefingMode) => {
    if (target === "morning") {
      setModeContent(null);
      setModeError("");
      setModeStatus("");
      setModeHasBriefing(false);
      setModeLoading(false);
      return;
    }

    setModeLoading(true);
    setModeError("");
    try {
      const response = await fetch(`${API}/db/briefings/modes/${target}/latest`);
      const data = (await response.json().catch(() => null)) as ModeLatestResponse | null;
      if (!response.ok) {
        throw new Error(
          extractDetail(data) ||
            `Failed to load ${target} briefing (${response.status})`
        );
      }
      const row = data?.briefing ?? null;
      const content =
        row && row.content && typeof row.content === "object"
          ? (row.content as BriefingContent)
          : null;
      const empty = data?.status === "empty" || !row || !content;
      setModeContent(empty ? null : content);
      setModeHasBriefing(!empty);
      setModeStatus(String(data?.status || (empty ? "empty" : "ok")));
    } catch (err) {
      setModeContent(null);
      setModeHasBriefing(false);
      setModeStatus("error");
      setModeError(err instanceof Error ? err.message : "Failed to load briefing");
    } finally {
      setModeLoading(false);
    }
  }, []);

  useEffect(() => {
    void loadModeLatest(mode);
  }, [mode, loadModeLatest]);

  const regenerateMode = useCallback(async () => {
    if (mode === "morning") {
      throw new Error("Use morning regenerate path");
    }
    const response = await fetch(
      `${API}/internal/briefings/modes/${mode}/generate`,
      { method: "POST" }
    );
    const data = await response.json().catch(() => null);
    if (!response.ok) {
      throw new Error(
        extractDetail(data) ||
          `Mode briefing generation failed (${response.status})`
      );
    }
    await loadModeLatest(mode);
  }, [mode, loadModeLatest]);

  const visibleModes = useMemo(() => {
    const all: BriefingMode[] = ["morning", "evening", "weekly", "monday"];
    return all.filter((id) => id !== "monday" || mondayVisible);
  }, [mondayVisible]);

  return {
    mode,
    setMode,
    timezone,
    mondayVisible,
    visibleModes,
    modeContent,
    modeLoading,
    modeError,
    modeStatus,
    modeHasBriefing,
    reloadMode: () => loadModeLatest(mode),
    regenerateMode,
  };
}
