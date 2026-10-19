import { Link } from "react-router-dom";
import BriefingPanel from "../BriefingPanel";
import "../home/Home.css";

export default function BriefingsPage() {
  return (
    <div className="home-briefings-page">
      <p style={{ marginBottom: 16 }}>
        <Link to="/">← Back to home</Link>
      </p>
      <BriefingPanel />
    </div>
  );
}
