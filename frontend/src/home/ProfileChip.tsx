import { useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { API } from "../api";

type Profile = {
  email: string;
  name: string | null;
  picture: string | null;
};

type CardPos = { top: number; right: number };

export default function ProfileChip() {
  const [profile, setProfile] = useState<Profile | null>(null);
  const [imgFailed, setImgFailed] = useState(false);
  const [open, setOpen] = useState(false);
  const [pos, setPos] = useState<CardPos>({ top: 0, right: 0 });
  const rootRef = useRef<HTMLDivElement>(null);
  const cardRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    let cancelled = false;
    fetch(`${API}/auth/me`)
      .then((res) => (res.ok ? res.json() : null))
      .then((data: Profile | null) => {
        if (!cancelled && data?.email) {
          setProfile(data);
        }
      })
      .catch(() => {
        /* Chip falls back to initials if identity is unavailable. */
      });
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    if (!open) return;

    const updatePos = () => {
      const chip = rootRef.current?.querySelector(".home-profile-chip");
      if (!(chip instanceof HTMLElement)) return;
      const rect = chip.getBoundingClientRect();
      setPos({
        top: rect.bottom + 8,
        right: window.innerWidth - rect.right,
      });
    };

    updatePos();

    const onPointerDown = (event: MouseEvent) => {
      const target = event.target as Node;
      if (rootRef.current?.contains(target) || cardRef.current?.contains(target)) {
        return;
      }
      setOpen(false);
    };
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        setOpen(false);
      }
    };

    document.addEventListener("mousedown", onPointerDown);
    document.addEventListener("keydown", onKeyDown);
    window.addEventListener("resize", updatePos);
    window.addEventListener("scroll", updatePos, true);
    return () => {
      document.removeEventListener("mousedown", onPointerDown);
      document.removeEventListener("keydown", onKeyDown);
      window.removeEventListener("resize", updatePos);
      window.removeEventListener("scroll", updatePos, true);
    };
  }, [open]);

  const email = profile?.email || "gaurang@rbhargavaassociates.in";
  const name = profile?.name?.trim() || "Gaurang";
  const picture = profile?.picture && !imgFailed ? profile.picture : null;
  const label = `Signed in as ${name}`;

  return (
    <div className="home-profile" ref={rootRef}>
      <button
        type="button"
        className="home-profile-chip"
        aria-label={label}
        aria-expanded={open}
        aria-haspopup="dialog"
        aria-controls="home-profile-card"
        onClick={() => setOpen((value) => !value)}
      >
        {picture ? (
          <img
            className="home-profile-photo"
            src={picture}
            alt=""
            width={40}
            height={40}
            referrerPolicy="no-referrer"
            onError={() => setImgFailed(true)}
          />
        ) : (
          <span className="home-profile-fallback" aria-hidden>
            G
          </span>
        )}
      </button>
      {open
        ? createPortal(
            <div
              id="home-profile-card"
              ref={cardRef}
              className="home-profile-card"
              role="dialog"
              aria-label="Account"
              style={{ top: pos.top, right: pos.right }}
            >
              <strong className="home-profile-name">{name}</strong>
              <span className="home-profile-email">{email}</span>
            </div>,
            document.body,
          )
        : null}
    </div>
  );
}
