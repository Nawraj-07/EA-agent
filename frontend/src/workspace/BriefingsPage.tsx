import { Link } from "react-router-dom";
import BriefingPanel from "../BriefingPanel";
import ProfileChip from "../home/ProfileChip";
import "../home/Home.css";

export default function BriefingsPage() {
  return (
    <div className="home-briefings-page">
      <div className="home-briefings-bar">
        <Link to="/">← Back to home</Link>
        <ProfileChip />
      </div>
      <BriefingPanel />
    </div>
  );
}
