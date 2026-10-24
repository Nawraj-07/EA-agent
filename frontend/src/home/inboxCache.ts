const STORAGE_KEY = "buddy.inboxCache.v1";

export type InboxCacheKey = {
  category: string;
  dateFrom: string | null;
  dateTo: string | null;
};

export type InboxCacheEntry<T> = {
  key: InboxCacheKey;
  conversations: T[];
  nextCursor: { internal_date: number; thread_id: string } | null;
};

type InboxCacheStore<T> = {
  lastKey: string;
  entries: Record<string, InboxCacheEntry<T>>;
};

const memory = new Map<string, InboxCacheEntry<unknown>>();
let lastKeyStr = "";

export function inboxCacheKey(
  category: string,
  dateFrom: string | null,
  dateTo: string | null
): InboxCacheKey {
  return { category, dateFrom, dateTo };
}

export function inboxCacheKeyString(key: InboxCacheKey): string {
  return `${key.category}|${key.dateFrom ?? ""}|${key.dateTo ?? ""}`;
}

function readStore<T>(): InboxCacheStore<T> | null {
  try {
    const raw = sessionStorage.getItem(STORAGE_KEY);
    if (!raw) return null;
    const parsed = JSON.parse(raw) as InboxCacheStore<T>;
    if (!parsed || typeof parsed !== "object" || !parsed.entries) return null;
    return parsed;
  } catch {
    return null;
  }
}

function persistStore<T>() {
  const entries: Record<string, InboxCacheEntry<T>> = {};
  for (const [key, value] of memory.entries()) {
    entries[key] = value as InboxCacheEntry<T>;
  }
  try {
    sessionStorage.setItem(
      STORAGE_KEY,
      JSON.stringify({ lastKey: lastKeyStr, entries } satisfies InboxCacheStore<T>)
    );
  } catch {
    // Ignore quota / private-mode failures; memory cache still works.
  }
}

function hydrateMemory<T>() {
  if (memory.size > 0) return;
  const store = readStore<T>();
  if (!store) return;
  lastKeyStr = store.lastKey || "";
  for (const [key, entry] of Object.entries(store.entries)) {
    if (entry && Array.isArray(entry.conversations)) {
      memory.set(key, entry);
    }
  }
}

export function readInboxCache<T>(key: InboxCacheKey): InboxCacheEntry<T> | null {
  hydrateMemory<T>();
  const entry = memory.get(inboxCacheKeyString(key));
  if (!entry || !Array.isArray(entry.conversations)) return null;
  return entry as InboxCacheEntry<T>;
}

export function readLastInboxCache<T>(): InboxCacheEntry<T> | null {
  hydrateMemory<T>();
  if (lastKeyStr) {
    const entry = memory.get(lastKeyStr);
    if (entry && Array.isArray(entry.conversations)) {
      return entry as InboxCacheEntry<T>;
    }
  }
  const first = memory.values().next().value;
  if (first && Array.isArray(first.conversations)) {
    return first as InboxCacheEntry<T>;
  }
  return null;
}

export function writeInboxCache<T>(entry: InboxCacheEntry<T>) {
  const keyStr = inboxCacheKeyString(entry.key);
  memory.set(keyStr, entry);
  lastKeyStr = keyStr;
  persistStore<T>();
}
