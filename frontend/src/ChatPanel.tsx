import { Fragment, useEffect, useRef, useState, type ReactNode } from "react";
import { API } from "./api";
const MAX_HISTORY_PAIRS = 5;

function stripMarkdownMarks(value: string): string {
  return value.replace(/\*\*|__/g, "");
}

function inlineAnswer(line: string, key: string): ReactNode[] {
  const parts: ReactNode[] = [];
  const pattern = /\*\*([^*]+)\*\*|__([^_]+)__/g;
  let last = 0;
  let match: RegExpExecArray | null;
  let index = 0;
  while ((match = pattern.exec(line))) {
    if (match.index > last) {
      parts.push(stripMarkdownMarks(line.slice(last, match.index)));
    }
    parts.push(
      <strong key={`${key}-b${index}`}>{match[1] || match[2]}</strong>
    );
    last = match.index + match[0].length;
    index += 1;
  }
  if (last < line.length) {
    parts.push(stripMarkdownMarks(line.slice(last)));
  }
  return parts;
}

function AnswerText({ text }: { text: string }) {
  const blocks = text.replace(/\r\n/g, "\n").split(/\n{2,}/);
  return (
    <div className="chat-answer">
      {blocks.map((block, blockIndex) => (
        <Fragment key={`block-${blockIndex}`}>{renderAnswerBlock(block, blockIndex)}</Fragment>
      ))}
    </div>
  );
}

function renderAnswerBlock(block: string, blockIndex: number): ReactNode[] {
  const lines = block.split("\n").filter((line) => line.trim());
  const nodes: ReactNode[] = [];
  let paragraph: string[] = [];
  let list: string[] = [];
  let listKind: "ol" | "ul" | null = null;
  let piece = 0;

  const flush = () => {
    if (listKind === "ol" && list.length) {
      nodes.push(
        <ol key={`ol-${blockIndex}-${piece}`}>
          {list.map((line, lineIndex) => (
            <li key={`oli-${blockIndex}-${lineIndex}`}>
              {inlineAnswer(line, `a${blockIndex}-${lineIndex}`)}
            </li>
          ))}
        </ol>
      );
    } else if (listKind === "ul" && list.length) {
      nodes.push(
        <ul key={`ul-${blockIndex}-${piece}`}>
          {list.map((line, lineIndex) => (
            <li key={`uli-${blockIndex}-${lineIndex}`}>
              {inlineAnswer(line, `a${blockIndex}-${lineIndex}`)}
            </li>
          ))}
        </ul>
      );
    } else if (paragraph.length) {
      nodes.push(
        <p key={`p-${blockIndex}-${piece}`}>
          {paragraph.map((line, lineIndex) => (
            <span key={`ln-${blockIndex}-${lineIndex}`}>
              {lineIndex > 0 ? <br /> : null}
              {inlineAnswer(line, `a${blockIndex}-${lineIndex}`)}
            </span>
          ))}
        </p>
      );
    }
    paragraph = [];
    list = [];
    listKind = null;
    piece += 1;
  };

  for (const line of lines) {
    if (/^\s*[-–]\s+/.test(line)) {
      if (listKind !== "ul") flush();
      listKind = "ul";
      list.push(line.replace(/^\s*[-–]\s+/, ""));
    } else if (/^\s*\d+\.\s+/.test(line)) {
      if (listKind !== "ol") flush();
      listKind = "ol";
      list.push(line.replace(/^\s*\d+\.\s+/, ""));
    } else if (listKind === "ol" && list.length) {
      list[list.length - 1] += ` ${line.trim()}`;
    } else {
      if (listKind) flush();
      paragraph.push(line);
    }
  }
  flush();
  return nodes;
}

type ChatSource = {
  email_id: string;
  subject?: string | null;
  sender?: string | null;
  gmail_thread_id?: string | null;
};

type ChatDraft = {
  id: string;
  email_id: string;
  body: string;
  status: string;
};

type PersonCandidate = {
  id: string;
  name: string;
  email: string;
  company?: string | null;
  last_interaction?: string;
  last_topic?: string;
  match_reason?: string;
  context?: string | null;
};

type PersonCard = {
  name: string;
  email?: string;
  addresses?: string[];
  summary?: string;
  last_interaction?: string;
  pending_actions?: string[];
  follow_up?: string;
  emails?: ChatSource[];
  sent_history_incomplete?: boolean;
  draft_email_id?: string | null;
  recommendation_key?: string | null;
  alias_note?: string | null;
};

type ChatTurn = {
  role: "user" | "assistant";
  content: string;
  sources?: ChatSource[];
  draft?: ChatDraft | null;
  candidates?: PersonCandidate[];
  person?: PersonCard | null;
  pending?: boolean;
  originalQuestion?: string;
  candidateOffset?: number;
  candidateHasMore?: boolean;
};

type SendOptions = {
  question?: string;
  display?: string;
  selectedContactId?: string | null;
  candidateOffset?: number;
};

export type OpenEmailOptions = {
  tab?: "draft" | "summary" | "thread";
  draft?: ChatDraft | null;
};

type ChatPanelProps = {
  emailId?: string | null;
  onOpenEmail?: (emailId: string, opts?: OpenEmailOptions) => void;
  initialQuestion?: string;
  /** Hide title/description when parent already provides chrome */
  embedded?: boolean;
  suggestions?: string[];
  /** Show expand control for a larger chat workspace */
  expandable?: boolean;
};

export default function ChatPanel({
  emailId,
  onOpenEmail,
  initialQuestion,
  embedded = false,
  suggestions,
  expandable = false,
}: ChatPanelProps) {
  const [question, setQuestion] = useState(initialQuestion || "");
  const [turns, setTurns] = useState<ChatTurn[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [expanded, setExpanded] = useState(false);
  const [personEnabled, setPersonEnabled] = useState(false);
  const [boundContactId, setBoundContactId] = useState<string | null>(null);
  const [actionError, setActionError] = useState("");
  const lastTurnRef = useRef<HTMLDivElement | null>(null);
  const inputRef = useRef<HTMLInputElement | null>(null);

  useEffect(() => {
    if (initialQuestion) {
      setQuestion(initialQuestion);
    }
  }, [initialQuestion]);

  useEffect(() => {
    let cancelled = false;
    fetch(`${API}/db/person-context/status`)
      .then((response) => (response.ok ? response.json() : null))
      .then((data) => {
        if (!cancelled) setPersonEnabled(Boolean(data?.enabled));
      })
      .catch(() => {
        if (!cancelled) setPersonEnabled(false);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    lastTurnRef.current?.scrollIntoView({
      behavior: "smooth",
      block: "nearest",
    });
  }, [turns, loading]);

  useEffect(() => {
    if (!expanded) return;
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") setExpanded(false);
    };
    document.addEventListener("keydown", onKey);
    const prevOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => {
      document.removeEventListener("keydown", onKey);
      document.body.style.overflow = prevOverflow;
    };
  }, [expanded]);

  async function sendQuestion(
    event?: React.FormEvent<HTMLFormElement>,
    opts?: SendOptions
  ) {
    event?.preventDefault();

    const trimmed = (opts?.question ?? question).trim();
    const display = (opts?.display ?? trimmed).trim();
    if (!trimmed || loading) return;

    const historyPayload = turns
      .filter((t) => !t.pending)
      .slice(-(MAX_HISTORY_PAIRS * 2))
      .map((t) => ({
        role: t.role,
        content: t.content,
        ...(t.role === "assistant" && t.sources?.length
          ? { sources: t.sources }
          : {}),
      }));

    setLoading(true);
    setError("");
    setQuestion("");
    setTurns((prev) => [
      ...prev,
      { role: "user", content: display },
      { role: "assistant", content: "Thinking…", pending: true },
    ]);
    inputRef.current?.focus();

    try {
      const response = await fetch(`${API}/chat`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
        },
        body: JSON.stringify({
          question: trimmed,
          email_id: emailId ?? null,
          history: historyPayload,
          selected_contact_id: opts?.selectedContactId || null,
          bound_contact_id: opts?.selectedContactId
            ? null
            : boundContactId,
          candidate_offset: opts?.candidateOffset || 0,
        }),
      });

      const data = (await response.json().catch(() => null)) as
        | {
            answer?: string;
            sources?: ChatSource[];
            draft?: ChatDraft | null;
            candidates?: PersonCandidate[];
            candidate_offset?: number;
            candidate_has_more?: boolean;
            person?: PersonCard | null;
            detail?: string;
          }
        | null;

      if (!response.ok) {
        const detail =
          data && typeof data.detail === "string" ? data.detail : null;
        throw new Error(detail || `Chat request failed (${response.status})`);
      }

      const answer = data?.answer || "No answer returned.";
      const sources = Array.isArray(data?.sources) ? data.sources : [];
      const draft =
        data?.draft &&
        typeof data.draft === "object" &&
        typeof data.draft.id === "string" &&
        typeof data.draft.email_id === "string"
          ? data.draft
          : null;
      const candidates =
        personEnabled && Array.isArray(data?.candidates) ? data.candidates : [];
      const person =
        personEnabled && data?.person && typeof data.person === "object"
          ? data.person
          : null;
      if (personEnabled && person?.email) {
        setBoundContactId(person.email);
      } else if (personEnabled && candidates.length > 0 && !opts?.selectedContactId) {
        setBoundContactId(null);
      }

      const assistantTurn: ChatTurn = {
        role: "assistant",
        content: answer,
        sources,
        draft,
        candidates,
        person,
        originalQuestion: candidates.length ? trimmed : undefined,
        candidateOffset: data?.candidate_offset || 0,
        candidateHasMore: Boolean(data?.candidate_has_more),
      };

      setTurns((prev) => {
        const next = [...prev];
        const last = next[next.length - 1];
        if (last?.pending) {
          next[next.length - 1] = assistantTurn;
          return next;
        }
        return [...next, assistantTurn];
      });
    } catch (err) {
      setTurns((prev) => prev.filter((turn) => !turn.pending));
      setError(err instanceof Error ? err.message : "Something went wrong");
    } finally {
      setLoading(false);
      inputRef.current?.focus();
    }
  }

  function applySuggestion(text: string) {
    setQuestion(text);
    inputRef.current?.focus();
  }

  async function draftReply(emailId: string) {
    setActionError("");
    try {
      const response = await fetch(`${API}/emails/${emailId}/draft`, {
        method: "POST",
      });
      const data = await response.json().catch(() => ({}));
      if (!response.ok) {
        throw new Error(
          typeof data.detail === "string" ? data.detail : "Draft failed"
        );
      }
      onOpenEmail?.(emailId, { tab: "draft", draft: data });
    } catch (err) {
      setActionError(err instanceof Error ? err.message : "Draft failed");
    }
  }

  async function runPersonFollowup(
    action: "draft" | "remind",
    key: string
  ) {
    setActionError("");
    try {
      if (action === "draft") {
        const response = await fetch(
          `${API}/db/briefings/recommendations/${encodeURIComponent(key)}/draft`,
          { method: "POST" }
        );
        const data = await response.json().catch(() => ({}));
        if (!response.ok) {
          throw new Error(
            typeof data.detail === "string" ? data.detail : "Draft failed"
          );
        }
        if (data.ok && data.draft?.email_id) {
          onOpenEmail?.(data.draft.email_id, { tab: "draft", draft: data.draft });
        }
        return;
      }
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
          typeof data.detail === "string" ? data.detail : "Remind failed"
        );
      }
    } catch (err) {
      setActionError(err instanceof Error ? err.message : "Action failed");
    }
  }

  const panelClass = [
    "chat-panel",
    embedded ? "chat-panel-embedded" : "",
    expanded ? "chat-panel-expanded" : "",
  ]
    .filter(Boolean)
    .join(" ");

  return (
    <>
      {expanded ? (
        <button
          type="button"
          className="chat-expand-backdrop"
          aria-label="Collapse chat"
          onClick={() => setExpanded(false)}
        />
      ) : null}
      <section
        className={panelClass}
        aria-expanded={expandable ? expanded : undefined}
      >
        {expanded ? (
          <div className="chat-expand-bar">
            <div>
              <h3>AI buddy</h3>
              <p>Larger workspace — Esc or Collapse to return</p>
            </div>
            <button
              type="button"
              className="chat-expand-toggle chat-expand-toggle-bar chat-expand-icon"
              onClick={() => setExpanded(false)}
              aria-label="Collapse chat"
              title="Collapse chat"
            >
              <span aria-hidden>×</span>
            </button>
          </div>
        ) : null}

        {!embedded && !expanded && (
          <>
            <h3>Ask My Inbox</h3>
            <p>
              Ask anything. Buddy uses your stored Inbox when the question is about mail.
            </p>
          </>
        )}

        {suggestions && suggestions.length > 0 && (
          <ul className="chat-suggestions">
            {suggestions.map((q) => (
              <li key={q}>
                <button
                  type="button"
                  disabled={loading}
                  onClick={() => applySuggestion(q)}
                >
                  <span>{q}</span>
                  <span className="chat-suggest-arrow" aria-hidden>
                    →
                  </span>
                </button>
              </li>
            ))}
          </ul>
        )}

        {turns.length > 0 && (
          <div className="chat-thread">
            {turns.map((turn, index) => {
              const isLast = index === turns.length - 1;
              return (
                <div
                  key={`${turn.role}-${index}`}
                  ref={isLast ? lastTurnRef : undefined}
                  className={`chat-turn chat-turn-${turn.role}${
                    turn.pending ? " chat-turn-pending" : ""
                  }`}
                >
                  <strong>{turn.role === "user" ? "You" : "AI"}</strong>
                  {turn.role === "assistant" && !turn.pending ? (
                    <AnswerText text={turn.content} />
                  ) : (
                    <p>{turn.content}</p>
                  )}
                  {turn.role === "assistant" &&
                  turn.candidates &&
                  turn.candidates.length > 0 ? (
                    <div className="chat-person-candidates">
                      {turn.candidates.map((candidate) => (
                        <button
                          key={candidate.id || candidate.email}
                          type="button"
                          className="chat-person-pick"
                          disabled={loading || !turn.originalQuestion}
                          onClick={() =>
                            void sendQuestion(undefined, {
                              question: turn.originalQuestion,
                              display: `Using ${candidate.name} <${candidate.email}>`,
                              selectedContactId: candidate.id || candidate.email,
                            })
                          }
                        >
                          <strong>{candidate.name}</strong>
                          <span>{candidate.email}</span>
                          {candidate.company ? (
                            <span>{candidate.company}</span>
                          ) : null}
                          {candidate.last_topic ? (
                            <span>Last topic: {candidate.last_topic}</span>
                          ) : null}
                          {candidate.last_interaction ? (
                            <span>Last interaction: {candidate.last_interaction}</span>
                          ) : null}
                        </button>
                      ))}
                      {turn.candidateHasMore && turn.originalQuestion ? (
                        <button
                          type="button"
                          className="chat-person-more"
                          disabled={loading}
                          onClick={() =>
                            void sendQuestion(undefined, {
                              question: turn.originalQuestion,
                              display: "Show more contacts",
                              candidateOffset: (turn.candidateOffset || 0) + 5,
                            })
                          }
                        >
                          Show more
                        </button>
                      ) : null}
                    </div>
                  ) : null}
                  {turn.role === "assistant" && turn.person ? (
                    <div className="chat-person-card">
                      <div className="chat-draft-card-head">
                        <span className="chat-draft-label">
                          {turn.person.name}
                        </span>
                        <span className="chat-draft-status">
                          {turn.person.email ||
                            (turn.person.addresses || []).join(", ")}
                        </span>
                      </div>
                      {turn.person.last_interaction ? (
                        <p className="chat-person-meta">
                          Last interaction: {turn.person.last_interaction}
                        </p>
                      ) : null}
                      {turn.person.pending_actions &&
                      turn.person.pending_actions.length > 0 ? (
                        <ul className="chat-person-list">
                          {turn.person.pending_actions.map((item) => (
                            <li key={item}>{item}</li>
                          ))}
                        </ul>
                      ) : null}
                      {turn.person.follow_up ? (
                        <p className="chat-person-meta">{turn.person.follow_up}</p>
                      ) : null}
                      {turn.person.sent_history_incomplete ? (
                        <p className="chat-person-meta">
                          Sent history may be incomplete.
                        </p>
                      ) : null}
                      {turn.person.alias_note ? (
                        <p className="chat-person-meta">{turn.person.alias_note}</p>
                      ) : null}
                      <div className="chat-person-actions">
                        {turn.person.draft_email_id ? (
                          <button
                            type="button"
                            className="chat-draft-open"
                            onClick={() =>
                              void draftReply(turn.person!.draft_email_id!)
                            }
                          >
                            Draft reply
                          </button>
                        ) : null}
                        {turn.person.recommendation_key ? (
                          <>
                            <button
                              type="button"
                              className="chat-draft-open"
                              onClick={() =>
                                void runPersonFollowup(
                                  "draft",
                                  turn.person!.recommendation_key!
                                )
                              }
                            >
                              Draft follow-up
                            </button>
                            <button
                              type="button"
                              className="chat-draft-open"
                              onClick={() =>
                                void runPersonFollowup(
                                  "remind",
                                  turn.person!.recommendation_key!
                                )
                              }
                            >
                              Remind tomorrow
                            </button>
                          </>
                        ) : null}
                      </div>
                      {turn.person.emails && turn.person.emails.length > 0 ? (
                        <div className="chat-sources">
                          <span className="chat-sources-label">Emails</span>
                          {turn.person.emails.map((src) => (
                            <button
                              key={src.email_id}
                              type="button"
                              className="chat-source-chip"
                              onClick={() => onOpenEmail?.(src.email_id)}
                              title={src.sender || undefined}
                            >
                              {src.subject || "Untitled email"}
                            </button>
                          ))}
                        </div>
                      ) : null}
                    </div>
                  ) : null}
                  {turn.role === "assistant" && turn.draft ? (
                    <div className="chat-draft-card">
                      <div className="chat-draft-card-head">
                        <span className="chat-draft-label">Draft reply</span>
                        <span className="chat-draft-status">
                          {turn.draft.status || "pending"}
                        </span>
                      </div>
                      <pre className="chat-draft-body">{turn.draft.body}</pre>
                      <button
                        type="button"
                        className="chat-draft-open"
                        onClick={() =>
                          onOpenEmail?.(turn.draft!.email_id, {
                            tab: "draft",
                            draft: turn.draft,
                          })
                        }
                      >
                        Open in Generate Draft
                      </button>
                    </div>
                  ) : null}
                  {turn.role === "assistant" &&
                    turn.sources &&
                    turn.sources.length > 0 && (
                      <div className="chat-sources">
                        <span className="chat-sources-label">Sources</span>
                        {turn.sources.map((src, index) => (
                          <button
                            key={src.email_id}
                            type="button"
                            className="chat-source-chip"
                            onClick={() => onOpenEmail?.(src.email_id)}
                            title={src.sender || undefined}
                          >
                            {(turn.sources?.length ?? 0) > 1
                              ? `${index + 1}. `
                              : ""}
                            {src.subject || "Untitled email"}
                          </button>
                        ))}
                      </div>
                    )}
                </div>
              );
            })}
          </div>
        )}

        <div className="chat-ask-row">
          <form className="chat-ask-form" onSubmit={(e) => void sendQuestion(e)}>
            <input
              ref={inputRef}
              type="text"
              value={question}
              onChange={(event) => setQuestion(event.target.value)}
              placeholder="Ask anything about your emails..."
              disabled={loading}
              aria-label="Ask your inbox"
            />
            <button type="submit" disabled={loading || !question.trim()}>
              {loading ? "Thinking…" : "Send"}
            </button>
          </form>
          {expandable ? (
            <button
              type="button"
              className={`chat-expand-toggle chat-expand-icon${
                expanded ? " chat-expand-icon-close" : ""
              }`}
              onClick={() => setExpanded((value) => !value)}
              aria-pressed={expanded}
              aria-label={expanded ? "Collapse chat" : "Expand chat"}
              title={expanded ? "Collapse chat" : "Expand chat"}
            >
              <span aria-hidden>{expanded ? "×" : "↗"}</span>
            </button>
          ) : null}
        </div>

        {error && <p role="alert">{error}</p>}
        {actionError && <p role="alert">{actionError}</p>}
      </section>
    </>
  );
}
