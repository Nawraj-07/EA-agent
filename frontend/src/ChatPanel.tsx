import { useEffect, useState } from "react";

const API = import.meta.env.VITE_API_URL || "http://localhost:8000";
const MAX_HISTORY_PAIRS = 5;

type ChatSource = {
  email_id: string;
  subject?: string | null;
  sender?: string | null;
  gmail_thread_id?: string | null;
};

type ChatTurn = {
  role: "user" | "assistant";
  content: string;
  sources?: ChatSource[];
};

type ChatPanelProps = {
  emailId?: string | null;
  onOpenEmail?: (emailId: string) => void;
  initialQuestion?: string;
  /** Hide title/description when parent already provides chrome */
  embedded?: boolean;
  suggestions?: string[];
};

export default function ChatPanel({
  emailId,
  onOpenEmail,
  initialQuestion,
  embedded = false,
  suggestions,
}: ChatPanelProps) {
  const [question, setQuestion] = useState(initialQuestion || "");
  const [turns, setTurns] = useState<ChatTurn[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    if (initialQuestion) {
      setQuestion(initialQuestion);
    }
  }, [initialQuestion]);

  async function sendQuestion(event?: React.FormEvent<HTMLFormElement>) {
    event?.preventDefault();

    const trimmed = question.trim();
    if (!trimmed || loading) return;

    setLoading(true);
    setError("");

    const historyPayload = turns.slice(-(MAX_HISTORY_PAIRS * 2)).map((t) => ({
      role: t.role,
      content: t.content,
    }));

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
        }),
      });

      const data = (await response.json().catch(() => null)) as
        | { answer?: string; sources?: ChatSource[]; detail?: string }
        | null;

      if (!response.ok) {
        const detail =
          data && typeof data.detail === "string" ? data.detail : null;
        throw new Error(detail || `Chat request failed (${response.status})`);
      }

      const answer = data?.answer || "No answer returned.";
      const sources = Array.isArray(data?.sources) ? data.sources : [];

      setTurns((prev) => [
        ...prev,
        { role: "user", content: trimmed },
        { role: "assistant", content: answer, sources },
      ]);
      setQuestion("");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Something went wrong");
    } finally {
      setLoading(false);
    }
  }

  function applySuggestion(text: string) {
    setQuestion(text);
  }

  return (
    <section className={`chat-panel${embedded ? " chat-panel-embedded" : ""}`}>
      {!embedded && (
        <>
          <h3>Ask My Inbox</h3>
          <p>
            {emailId
              ? "Asking about the selected email (follow-ups supported)"
              : "Search across saved mail by sender, date, keywords, importance, or action needed"}
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
          {turns.map((turn, index) => (
            <div
              key={`${turn.role}-${index}`}
              className={`chat-turn chat-turn-${turn.role}`}
            >
              <strong>{turn.role === "user" ? "You" : "AI"}</strong>
              <p>{turn.content}</p>
              {turn.role === "assistant" &&
                turn.sources &&
                turn.sources.length > 0 && (
                  <div className="chat-sources">
                    <span className="chat-sources-label">Sources</span>
                    {turn.sources.map((src) => (
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
                )}
            </div>
          ))}
        </div>
      )}

      <form className="chat-ask-form" onSubmit={(e) => void sendQuestion(e)}>
        <input
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

      {error && <p role="alert">{error}</p>}
    </section>
  );
}
