import { useEffect, useRef, useState } from "react";
import { ApiError, instrumentsApi } from "../../api/client";
import * as theme from "../../theme";

const POLL_INTERVAL_MS = 800;

type PreviewStatus = "loading" | "ready" | "unavailable" | "error";

export default function CameraPreview({ instrument }: { instrument: string }) {
  const [imageUrl, setImageUrl] = useState<string | null>(null);
  const [status, setStatus] = useState<PreviewStatus>("loading");
  const [message, setMessage] = useState<string | null>(null);
  const imageUrlRef = useRef<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    let timeoutId: ReturnType<typeof setTimeout> | null = null;

    async function tick() {
      if (cancelled) return;
      if (document.visibilityState !== "hidden") {
        try {
          await instrumentsApi.captureCameraFrame(instrument, true);
          const blob = await instrumentsApi.cameraLastImage(instrument);
          if (cancelled) return;
          const url = URL.createObjectURL(blob);
          if (imageUrlRef.current) URL.revokeObjectURL(imageUrlRef.current);
          imageUrlRef.current = url;
          setImageUrl(url);
          setStatus("ready");
          setMessage(null);
        } catch (err) {
          if (cancelled) return;
          if (err instanceof ApiError && err.status === 501) {
            // Vendor doesn't implement capture at all (e.g. mount_only) —
            // retrying on an interval would just error forever.
            setStatus("unavailable");
            setMessage(err.message);
            return;
          }
          setStatus("error");
          setMessage(err instanceof Error ? err.message : String(err));
        }
      }
      if (!cancelled) {
        timeoutId = setTimeout(tick, POLL_INTERVAL_MS);
      }
    }

    void tick();

    return () => {
      cancelled = true;
      if (timeoutId) clearTimeout(timeoutId);
      if (imageUrlRef.current) {
        URL.revokeObjectURL(imageUrlRef.current);
        imageUrlRef.current = null;
      }
      setImageUrl(null);
    };
  }, [instrument]);

  return (
    <div style={containerStyle}>
      {imageUrl ? (
        <img src={imageUrl} alt={`Live preview from ${instrument}`} style={imageStyle} />
      ) : (
        <div style={placeholderStyle}>
          {status === "unavailable"
            ? (message ?? "Live preview isn't available for this camera.")
            : status === "error"
              ? (message ?? "Preview failed.")
              : "Loading preview…"}
        </div>
      )}
      <svg viewBox="0 0 100 100" preserveAspectRatio="none" style={crosshairStyle} aria-hidden="true">
        <line x1="50" y1="0" x2="50" y2="100" stroke={CROSSHAIR_COLOR} strokeWidth="0.6" />
        <line x1="0" y1="50" x2="100" y2="50" stroke={CROSSHAIR_COLOR} strokeWidth="0.6" />
      </svg>
      {status === "error" && imageUrl && <div style={errorBannerStyle}>{message}</div>}
    </div>
  );
}

const CROSSHAIR_COLOR = theme.color.danger;

const containerStyle: React.CSSProperties = {
  position: "relative",
  width: "100%",
  aspectRatio: "4 / 3",
  background: theme.color.surfaceSunken,
  border: `1px solid ${theme.color.border}`,
  borderRadius: 8,
  overflow: "hidden",
  marginBottom: 12,
};

const imageStyle: React.CSSProperties = {
  width: "100%",
  height: "100%",
  objectFit: "contain",
  display: "block",
};

const placeholderStyle: React.CSSProperties = {
  position: "absolute",
  inset: 0,
  display: "flex",
  alignItems: "center",
  justifyContent: "center",
  textAlign: "center",
  padding: 16,
  fontSize: 13,
  color: theme.color.textMuted,
};

const crosshairStyle: React.CSSProperties = {
  position: "absolute",
  inset: 0,
  width: "100%",
  height: "100%",
  pointerEvents: "none",
};

const errorBannerStyle: React.CSSProperties = {
  position: "absolute",
  left: 0,
  right: 0,
  bottom: 0,
  padding: "4px 8px",
  fontSize: 11,
  color: "#fff",
  background: "rgba(220, 38, 38, 0.85)",
};
