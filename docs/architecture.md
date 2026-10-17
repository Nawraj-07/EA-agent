# Gmail AI Executive Assistant — System Architecture

## Step A — Overall Architecture

The application follows a layered architecture where React handles the frontend, FastAPI manages backend operations, external APIs provide Gmail and AI functionality, and Supabase stores data.

**Architecture Flow:**

React → FastAPI → Gmail API → OpenAI API → Supabase → FastAPI → React Dashboard

## Step B — Architecture Explanation

### 1. What Does the Application Do?

The Gmail AI Executive Assistant connects to Gmail, fetches emails, and uses AI to generate summaries, identify important messages, and extract deadlines.

Users can view email summaries, notifications, and reminders through a centralized web dashboard.

### 2. Components and Responsibilities

- **React:** Provides the dashboard for emails, summaries, notifications, and reminders.
- **FastAPI:** Handles API requests, business logic, authentication, and communication with external services.
- **Gmail API + OAuth 2.0:** Securely authorizes Gmail access and retrieves emails.
- **OpenAI API:** Summarizes emails, classifies priorities, and extracts important information.
- **Supabase PostgreSQL:** Stores user data, email metadata, AI summaries, notifications, and encrypted OAuth tokens.
- **APScheduler:** Periodically checks Gmail for new emails and triggers processing.
- **LangChain :** Integrates AI tools and manages advanced agent workflows.

### 3. Example Workflow

**When a user opens the dashboard:**

1. React sends a request to FastAPI.
2. FastAPI retrieves stored email summaries from Supabase.
3. FastAPI returns the data as JSON.
4. React displays the summaries and notifications.

**Background Email Processing:**

APScheduler → FastAPI → Gmail API → OpenAI API → Supabase

The scheduler triggers email synchronization, FastAPI fetches new emails, OpenAI analyzes them, and Supabase stores the results.

### 4. Secret Management

- `OPENAI_API_KEY`: Stored in backend environment variables.
- **Google OAuth refresh tokens:** Stored encrypted in Supabase and accessed only by the backend.
- **React:** Never stores or exposes private API keys or refresh tokens.

## Step C — Security Checkpoint

**Question:** Which service owns Google refresh tokens and `OPENAI_API_KEY`, and why?

**Answer:**

The **FastAPI backend** manages both secrets because it communicates securely with Gmail and OpenAI.

The OpenAI API key is stored in backend environment variables, while Google refresh tokens are encrypted and stored in Supabase.

Secrets must remain on the backend because frontend code can be inspected through browser DevTools.

## Architecture Summary

**React = UI | FastAPI = Backend Logic | Gmail API = Emails | OpenAI = AI Processing | Supabase = Database | APScheduler = Automation**