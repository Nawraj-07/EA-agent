import { useRef, useState, type MouseEvent } from "react";
import { API } from "../api";
import {
  formatBriefingDate,
  normalizeBriefing,
  senderName,
  type BriefingContent,
  type StructuredBriefingItem,
} from "./briefingTypes";
import { isMeetingUrl } from "./meetingLink";

type OpenEmailOpts = {
  tab?: "summary" | "thread" | "draft";
  draft?: {
    id: string;
    email_id: string;
    body: string;
    status: string;
  } | null;
};

type DailyBriefingBodyProps = {
  content?: BriefingContent | null;
  onOpenEmail: (emailId: string, opts?: OpenEmailOpts) => void;
  showTodayOverview?: boolean;
};

function PriorityBadge({ value }: { value?: string }) {
  const priority = (value || "").toLowerCase();
  if (priority !== "high" && priority !== "medium" && priority !== "low") {
    return null;
  }
  return (
    <span className={`email-glance-tab priority ${priority}`}>{priority}</span>
  );
}

function SourceMeta({ sender }: { sender?: string }) {
  if (!sender) return null;
  return (
    <p className="briefing-source-meta">
      From {senderName(sender)}
    </p>
  );
}

function focusLine(followupCount: number): string {
  if (followupCount === 0) {
    return "Nothing is waiting on a reply.";
  }
  const noun = followupCount === 1 ? "follow-up" : "follow-ups";
  return `Reply to the ${followupCount} ${noun} and clear today's pending actions.`;
}

function scrollPanelTo(target: HTMLElement) {
  let node: HTMLElement | null = target.parentElement;
  while (node) {
    const style = window.getComputedStyle(node);
    const scrollable =
      (style.overflowY === "auto" || style.overflowY === "scroll") &&
      node.scrollHeight > node.clientHeight + 8;
    if (scrollable) {
      const top =
        target.getBoundingClientRect().top -
        node.getBoundingClientRect().top +
        node.scrollTop -
        8;
      node.scrollTo({ top: Math.max(0, top), behavior: "smooth" });
      return;
    }
    node = node.parentElement;
  }
  target.scrollIntoView({ behavior: "smooth", block: "start" });
}

function isFollowupItem(item: StructuredBriefingItem): boolean {
  const kind = (item.kind || "").toLowerCase();
  return (
    Boolean(item.followup_state) ||
    kind === "followup" ||
    kind === "pending_reply"
  );
}

function FollowupActions({
  item,
  onOpenEmail,
  onHidden,
}: {
  item: StructuredBriefingItem;
  onOpenEmail: (emailId: string, opts?: OpenEmailOpts) => void;
  onHidden: () => void;
}) {
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState("");
  const key = item.recommendation_key || item.recommendation_id;
  const emailId = item.source_email_id;
  const enabled = Boolean(item.followup_actions_enabled && key);

  if (!enabled) return null;

  async function run(
    action: "draft" | "remind" | "dismiss",
    event: MouseEvent
  ) {
    event.preventDefault();
    event.stopPropagation();
    if (!key || busy) return;
    setBusy(action);
    setError("");
    try {
      if (action === "draft") {
        const response = await fetch(
          `${API}/db/briefings/recommendations/${encodeURIComponent(key)}/draft`,
          { method: "POST" }
        );
        const data = await response.json().catch(() => ({}));
        if (!response.ok) {
          throw new Error(
            typeof data.detail === "string"
              ? data.detail
              : `Draft failed (${response.status})`
          );
        }
        if (data.ok && data.draft?.email_id) {
          onOpenEmail(data.draft.email_id, {
            tab: "draft",
            draft: data.draft,
          });
        } else if (emailId) {
          onOpenEmail(emailId, { tab: "draft" });
        }
        return;
      }
      if (action === "remind") {
        const response = await fetch(
          `${API}/db/briefings/recommendations/${encodeURIComponent(key)}/snooze`,
          {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ preset: "tomorrow" }),
          }
        );
        const data = await response.json().catch(() => ({}));
        if (!response.ok) {
          throw new Error(
            typeof data.detail === "string"
              ? data.detail
              : `Remind failed (${response.status})`
          );
        }
        onHidden();
        return;
      }
      const response = await fetch(
        `${API}/db/briefings/recommendations/${encodeURIComponent(key)}/status`,
        {
          method: "PATCH",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ status: "dismissed" }),
        }
      );
      const data = await response.json().catch(() => ({}));
      if (!response.ok) {
        throw new Error(
          typeof data.detail === "string"
            ? data.detail
            : `Dismiss failed (${response.status})`
        );
      }
      onHidden();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Action failed");
    } finally {
      setBusy(null);
    }
  }

  return (
    <div className="briefing-followup-actions">
      <button
        type="button"
        className="briefing-followup-btn"
        disabled={Boolean(busy)}
        onClick={(event) => void run("draft", event)}
      >
        {busy === "draft" ? "Drafting…" : "Draft Follow-up"}
      </button>
      <button
        type="button"
        className="briefing-followup-btn"
        disabled={Boolean(busy)}
        onClick={(event) => void run("remind", event)}
      >
        {busy === "remind" ? "Saving…" : "Remind Tomorrow"}
      </button>
      <button
        type="button"
        className="briefing-followup-btn muted"
        disabled={Boolean(busy)}
        onClick={(event) => void run("dismiss", event)}
      >
        {busy === "dismiss" ? "…" : "Dismiss"}
      </button>
      {error ? <span className="briefing-followup-error">{error}</span> : null}
    </div>
  );
}

export default function DailyBriefingBody({
  content,
  onOpenEmail,
  showTodayOverview = false,
}: DailyBriefingBodyProps) {
  const briefing = normalizeBriefing(content);
  const [hiddenKeys, setHiddenKeys] = useState<Record<string, true>>({});
  const [activeSection, setActiveSection] = useState<string | null>(null);
  const sections = useRef<Record<string, HTMLElement | null>>({});

  function jumpTo(section: string) {
    const target = sections.current[section];
    if (!target) return;
    scrollPanelTo(target);
    setActiveSection(section);
    window.setTimeout(() => {
      setActiveSection((current) => (current === section ? null : current));
    }, 1400);
  }

  function openIfPossible(emailId?: string | null) {
    if (!emailId) return;
    onOpenEmail(emailId);
  }

  function itemKey(item: StructuredBriefingItem, index: number) {
    return (
      item.recommendation_key ||
      item.recommendation_id ||
      item.source_email_id ||
      `${item.title}-${index}`
    );
  }

  const visibleItems = briefing.items
    .map((item, index) => ({ item, index, key: itemKey(item, index) }))
    .filter((row) => !hiddenKeys[row.key]);
  const followups = visibleItems.filter((row) => isFollowupItem(row.item));
  const priorities = visibleItems.filter((row) => !isFollowupItem(row.item));
  const replyCount = followups.length;

  return (
    <div className="briefing-structured">
      {showTodayOverview ? (
        <section className="briefing-section briefing-overview" aria-label="Today's overview">
          <h3>Today&apos;s Overview</h3>
          <p className="briefing-overview-lead">
            Here&apos;s what matters today based on your emails.
          </p>
          <div className="briefing-stat-row">
            <button
              type="button"
              className="briefing-stat"
              onClick={() => jumpTo("priorities")}
            >
              <strong>{briefing.importantUpdates}</strong>
              <span>Important Updates</span>
            </button>
            <button
              type="button"
              className="briefing-stat"
              onClick={() => jumpTo("followups")}
            >
              <strong>{briefing.pendingActions}</strong>
              <span>Pending Actions</span>
            </button>
            <button
              type="button"
              className="briefing-stat"
              onClick={() => jumpTo("meetings")}
            >
              <strong>{briefing.meetingCount}</strong>
              <span>Meetings</span>
            </button>
          </div>
        </section>
      ) : null}

      {showTodayOverview && briefing.notes ? (
        <p className="briefing-notes-inline">{briefing.notes}</p>
      ) : null}

      <section
        className={`briefing-section briefing-priorities${
          activeSection === "priorities" ? " is-targeted" : ""
        }`}
        aria-label="Priority briefings"
        ref={(node) => {
          sections.current.priorities = node;
        }}
      >
        <h3>Priority Briefings</h3>
        {priorities.length === 0 ? (
          <p className="briefing-empty-line">No priority items for this day.</p>
        ) : (
          <ul className="briefing-card-list">
            {priorities.map(({ item, index }) => {
              const clickable = Boolean(item.source_email_id);
              const inner = (
                <>
                  <div className="briefing-card-top">
                    <PriorityBadge value={item.priority} />
                    <strong>{item.title}</strong>
                  </div>
                  {item.description ? (
                    <p className="briefing-card-copy">{item.description}</p>
                  ) : null}
                  {item.recommended_action ? (
                    <p className="briefing-card-action">
                      <span className="briefing-card-action-label">Do</span>
                      <strong>{item.recommended_action}</strong>
                    </p>
                  ) : null}
                  <SourceMeta sender={item.sender} />
                </>
              );
              return (
                <li key={`${item.source_email_id || item.title}-${index}`}>
                  {clickable ? (
                    <button
                      type="button"
                      className="briefing-card clickable"
                      onClick={() => openIfPossible(item.source_email_id)}
                    >
                      {inner}
                    </button>
                  ) : (
                    <div className="briefing-card">{inner}</div>
                  )}
                </li>
              );
            })}
          </ul>
        )}
      </section>

      <section
        className={`briefing-section briefing-followups${
          activeSection === "followups" ? " is-targeted" : ""
        }`}
        aria-label="Key follow-ups"
        ref={(node) => {
          sections.current.followups = node;
        }}
      >
        <div className="briefing-section-head">
          <h3>Key Follow-ups</h3>
          {replyCount > 0 ? (
            <span className="briefing-reply-badge">
              {replyCount} to Reply
            </span>
          ) : null}
        </div>
        {replyCount === 0 ? (
          <p className="briefing-empty-line">No follow-ups waiting on a reply.</p>
        ) : (
          <ul className="briefing-card-list">
            {followups.map(({ item, index, key }) => {
              const clickable = Boolean(item.source_email_id);
              const row = (
                <div className="briefing-followup-row">
                  <div className="briefing-followup-copy">
                    <strong>{item.title}</strong>
                    {item.description ? <p>{item.description}</p> : null}
                  </div>
                  <PriorityBadge value={item.priority} />
                </div>
              );
              return (
                <li key={`${item.source_email_id || item.title}-${index}`}>
                  <div className={`briefing-card${clickable ? " clickable" : ""}`}>
                    {clickable ? (
                      <button
                        type="button"
                        className="briefing-card-main"
                        onClick={() => openIfPossible(item.source_email_id)}
                      >
                        {row}
                      </button>
                    ) : (
                      row
                    )}
                    <FollowupActions
                      item={item}
                      onOpenEmail={onOpenEmail}
                      onHidden={() =>
                        setHiddenKeys((prev) => ({ ...prev, [key]: true }))
                      }
                    />
                  </div>
                </li>
              );
            })}
          </ul>
        )}
        <p className="briefing-focus">{focusLine(replyCount)}</p>
      </section>

      <section
        className={`briefing-section briefing-meetings${
          activeSection === "meetings" ? " is-targeted" : ""
        }`}
        aria-label="Meetings and events today"
        ref={(node) => {
          sections.current.meetings = node;
        }}
      >
        <h3>Meetings &amp; Events Today</h3>
        {briefing.meetings.length === 0 ? (
          <p className="briefing-empty-line">
            No meetings or events found in your emails for today.
          </p>
        ) : (
          <ul className="briefing-card-list">
            {briefing.meetings.map((meeting, index) => {
              const formatClock = (value?: string) => {
                if (!value) return "";
                const match = /^(\d{1,2}):(\d{2})$/.exec(value.trim());
                if (!match) return value;
                const hour = Number(match[1]);
                const minute = match[2];
                const suffix = hour >= 12 ? "PM" : "AM";
                const hour12 = ((hour + 11) % 12) + 1;
                return `${hour12}:${minute} ${suffix}`;
              };
              const startLabel = formatClock(meeting.start_time);
              const endLabel = formatClock(meeting.end_time);
              const timeLabel = startLabel
                ? endLabel
                  ? `${startLabel} – ${endLabel}`
                  : startLabel
                : "Time not specified";
              const clickable = Boolean(meeting.source_email_id);
              const joinUrl = isMeetingUrl(meeting.location)
                ? meeting.location!.trim()
                : null;
              return (
                <li key={`${meeting.source_email_id || meeting.event_title}-${index}`}>
                  <div
                    className={`briefing-card${
                      meeting.status === "cancelled" ? " cancelled" : ""
                    }${clickable ? " clickable" : ""}`}
                  >
                    {clickable ? (
                      <button
                        type="button"
                        className="briefing-card-main"
                        onClick={() => openIfPossible(meeting.source_email_id)}
                      >
                        <div className="briefing-card-top">
                          <strong>{meeting.event_title}</strong>
                          {meeting.status ? (
                            <span className={`briefing-status ${meeting.status}`}>
                              {meeting.status}
                            </span>
                          ) : null}
                        </div>
                        <p className="briefing-card-copy">
                          {formatBriefingDate(meeting.event_date)} · {timeLabel}
                        </p>
                        <SourceMeta
                          sender={meeting.organizer || meeting.sender}
                        />
                      </button>
                    ) : (
                      <>
                        <div className="briefing-card-top">
                          <strong>{meeting.event_title}</strong>
                          {meeting.status ? (
                            <span className={`briefing-status ${meeting.status}`}>
                              {meeting.status}
                            </span>
                          ) : null}
                        </div>
                        <p className="briefing-card-copy">
                          {formatBriefingDate(meeting.event_date)} · {timeLabel}
                        </p>
                        <SourceMeta
                          sender={meeting.organizer || meeting.sender}
                        />
                      </>
                    )}
                    {joinUrl ? (
                      <a
                        className="briefing-join-link"
                        href={joinUrl}
                        target="_blank"
                        rel="noopener noreferrer"
                        onClick={(event) => event.stopPropagation()}
                      >
                        Join meeting
                      </a>
                    ) : meeting.location ? (
                      <p className="briefing-card-copy briefing-location-text">
                        {meeting.location}
                      </p>
                    ) : null}
                  </div>
                </li>
              );
            })}
          </ul>
        )}
      </section>

      <section className="briefing-section briefing-deadlines" aria-label="Upcoming deadlines">
        <h3>Upcoming Deadlines</h3>
        {briefing.deadlines.length === 0 ? (
          <p className="briefing-empty-line">No upcoming deadlines found.</p>
        ) : (
          <ul className="briefing-card-list">
            {briefing.deadlines.map((item, index) => {
              const clickable = Boolean(item.source_email_id);
              const inner = (
                <>
                  <div className="briefing-card-top">
                    <PriorityBadge value={item.priority} />
                    <strong>{item.title}</strong>
                    {item.overdue ? (
                      <span className="briefing-status cancelled">overdue</span>
                    ) : null}
                  </div>
                  <p className="briefing-card-copy">
                    Due {item.due_date ? formatBriefingDate(item.due_date) : "date not specified"}
                  </p>
                  <SourceMeta sender={item.sender} />
                </>
              );
              return (
                <li key={`${item.source_email_id || item.title}-${index}`}>
                  {clickable ? (
                    <button
                      type="button"
                      className="briefing-card clickable"
                      onClick={() => openIfPossible(item.source_email_id)}
                    >
                      {inner}
                    </button>
                  ) : (
                    <div className="briefing-card">{inner}</div>
                  )}
                </li>
              );
            })}
          </ul>
        )}
      </section>
    </div>
  );
}
