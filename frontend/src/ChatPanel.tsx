
import { useState } from "react";

const API = import.meta.env.VITE_API_URL || "http://localhost:8000";

type ChatPanelProps = {
  emailId?: string | null;
};

export default function ChatPanel({ emailId }: ChatPanelProps) {
  const [question, setQuestion] = useState("");
  const [answer, setAnswer] = useState("");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");

  async function sendQuestion(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();

    if (!question.trim() || loading) return;

    setLoading(true);
    setError("");
    setAnswer("");

    try {
      const response = await fetch(`${API}/chat`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
        },
        body: JSON.stringify({
          question: question.trim(),
          email_id: emailId ?? null,
        }),
      });

      if (!response.ok) {
        throw new Error(`Chat request failed (${response.status})`);
      }

      const data: { answer: string } = await response.json();

      setAnswer(data.answer);
    } catch (err) {
      setError(
        err instanceof Error ? err.message : "Something went wrong"
      );
    } finally {
      setLoading(false);
    }
  }

  return (
    <section className="chat-panel">
      <h3>Ask AI about your emails</h3>

      <p>
        {emailId
          ? "Asking about the selected email"
          : "Asking about your recent saved emails"}
      </p>

      <form onSubmit={sendQuestion}>
        <input
          type="text"
          value={question}
          onChange={(event) => setQuestion(event.target.value)}
          placeholder="What is this email about?"
          disabled={loading}
        />

        <button type="submit" disabled={loading || !question.trim()}>
          {loading ? "Thinking..." : "Send"}
        </button>
      </form>

      {error && <p role="alert">{error}</p>}

      {answer && (
        <div className="chat-answer">
          <strong>AI Answer</strong>
          <p>{answer}</p>
        </div>
      )}
    </section>
  );
}
