# Briefing cache across page navigation

## Problem

`App.tsx` remounts `HomePage` on every visit to `/`. That re-runs:

- `useBriefingHistory` → always `GET /db/briefings/history` (Morning)
- `useBriefingMode` → always `GET /db/briefings/modes/{mode}/latest` when mode ≠ morning

Inbox already avoids this with `inboxCache.ts` (memory + `sessionStorage`). Briefings do not.

## Approach (locked)

Add a **session briefing cache** modeled on inbox cache:

- Module memory map (survives remount while SPA is open)
- Mirror into `sessionStorage` (survives soft reloads in the same tab)
- **No automatic refetch** when returning to Home if a cache entry exists
- **Force refetch** only on: Regenerate, Refresh Inbox, or explicit cache miss / first visit

Do **not** keep Home mounted via route layout changes. Cache matches existing patterns.

## Implementation

### 1. New `frontend/src/home/briefingCache.ts`

Store: history, selectedDate, selectedMode, per-mode latest payloads.

API: read/write history, mode, selection; invalidate helpers for regenerate/refresh.

### 2. Update `useBriefingHistory.ts`

- Hydrate from cache on mount; skip network on hit.
- `reload(..., { force })` for generate/sync paths.

### 3. Update `useBriefingMode.ts`

- Restore selected mode from cache (respect Monday visibility).
- Cache-first mode latest; force on regenerate/reloadMode.

### 4. Wire `HomePage.tsx`

- Refresh Inbox / Regenerate force-update cache.
- Filter switch uses cache (no GET if already loaded).

## Done when

Returning to Home shows last Morning/Evening/Weekly (Monday when visible) instantly without refetch, unless Regenerate or Refresh Inbox.
