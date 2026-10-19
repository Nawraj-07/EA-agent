import { BrowserRouter, Navigate, Route, Routes } from "react-router-dom";
import HomePage from "./home/HomePage";
import BriefingsPage from "./workspace/BriefingsPage";
import "./App.css";

function App() {
  return (
    <BrowserRouter>
      <Routes>
        <Route index element={<HomePage />} />
        <Route path="briefings" element={<BriefingsPage />} />
        <Route path="*" element={<Navigate to="/" replace />} />
      </Routes>
    </BrowserRouter>
  );
}

export default App;
