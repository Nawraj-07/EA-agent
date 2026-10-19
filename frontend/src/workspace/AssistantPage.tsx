import { useLocation } from "react-router-dom";
import ChatPanel from "../ChatPanel";

type LocationState = { question?: string } | null;

export default function AssistantPage() {
  const location = useLocation();
  const state = (location.state as LocationState) || null;

  return (
    <div className="ea-panel-page">
      <ChatPanel initialQuestion={state?.question || ""} />
    </div>
  );
}
