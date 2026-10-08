import { useCallback, useEffect, useRef, useState } from "react";
import { ApiError, instrumentsApi } from "../../api/client";
import * as theme from "../../theme";
import { containedImageRect, pointInImage } from "./cameraGeometry";
import type { ImageContentRect, NormalizedPoint } from "./cameraGeometry";

const POLL_INTERVAL_MS = 800;

type PreviewStatus = "loading" | "ready" | "unavailable" | "error";

interface CameraPreviewProps {
  instrument: string;
  detectedCenter?: NormalizedPoint | null;
  onProposedCenterChange?: (point: NormalizedPoint | null) => void;
}

export default function CameraPreview({
  instrument,
  detectedCenter = null,
  onProposedCenterChange,
}: CameraPreviewProps) {
  const [imageUrl, setImageUrl] = useState<string | null>(null);
  const [status, setStatus] = useState<PreviewStatus>("loading");
  const [message, setMessage] = useState<string | null>(null);
  const [contentRect, setContentRect] = useState<ImageContentRect | null>(null);
  const [proposedCenter, setProposedCenter] = useState<NormalizedPoint | null>(null);
  const imageUrlRef = useRef<string | null>(null);
  const containerRef = useRef<HTMLDivElement | null>(null);
  const imageRef = useRef<HTMLImageElement | null>(null);

  const measureImage = useCallback(() => {
    const container = containerRef.current;
    const image = imageRef.current;
    if (!container || !image) return;
    const bounds = container.getBoundingClientRect();
    setContentRect(containedImageRect(bounds.width, bounds.height, image.naturalWidth, image.naturalHeight));
  }, []);

  useEffect(() => {
    const container = containerRef.current;
    if (!container || typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(measureImage);
    observer.observe(container);
    return () => observer.disconnect();
  }, [measureImage]);

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
            setStatus("unavailable");
            setMessage(err.message);
            return;
          }
          setStatus("error");
          setMessage(err instanceof Error ? err.message : String(err));
        }
      }
      if (!cancelled) timeoutId = setTimeout(tick, POLL_INTERVAL_MS);
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

  const proposeCenter = (event: React.MouseEvent<HTMLDivElement>) => {
    if (!contentRect || !containerRef.current || !imageUrl) return;
    const bounds = containerRef.current.getBoundingClientRect();
    const point = pointInImage(event.clientX, event.clientY, bounds.left, bounds.top, contentRect);
    if (!point) return;
    setProposedCenter(point);
    onProposedCenterChange?.(point);
  };

  const clearProposal = (event: React.MouseEvent<HTMLButtonElement>) => {
    event.stopPropagation();
    setProposedCenter(null);
    onProposedCenterChange?.(null);
  };

  const overlayRect = contentRect ?? { left: 0, top: 0, width: 0, height: 0 };
  const offset = proposedCenter
    ? { x: (proposedCenter.x - 0.5) * 100, y: (proposedCenter.y - 0.5) * 100 }
    : null;

  return (
    <div>
      <div
        ref={containerRef}
        style={{ ...containerStyle, cursor: imageUrl ? "crosshair" : "default" }}
        onClick={proposeCenter}
        aria-label="Camera image review"
      >
        {imageUrl ? (
          <img
            ref={imageRef}
            src={imageUrl}
            alt={`Live preview from ${instrument}`}
            style={imageStyle}
            onLoad={measureImage}
          />
        ) : (
          <div style={placeholderStyle}>
            {status === "unavailable"
              ? (message ?? "Live preview isn't available for this camera.")
              : status === "error"
                ? (message ?? "Preview failed.")
                : "Loading preview…"}
          </div>
        )}
        {imageUrl && contentRect && (
          <svg
            data-testid="camera-image-overlay"
            viewBox="0 0 100 100"
            preserveAspectRatio="none"
            style={{ ...crosshairStyle, ...overlayRect }}
            aria-hidden="true"
          >
            <line x1="50" y1="0" x2="50" y2="100" stroke={OPTICAL_CENTER_COLOR} strokeWidth="0.6" />
            <line x1="0" y1="50" x2="100" y2="50" stroke={OPTICAL_CENTER_COLOR} strokeWidth="0.6" />
            {detectedCenter && <circle cx={detectedCenter.x * 100} cy={detectedCenter.y * 100} r="2.4" fill="none" stroke={DETECTED_CENTER_COLOR} strokeWidth="0.9" />}
            {proposedCenter && <circle cx={proposedCenter.x * 100} cy={proposedCenter.y * 100} r="2.4" fill="none" stroke={PROPOSED_CENTER_COLOR} strokeWidth="0.9" />}
          </svg>
        )}
        {status === "error" && imageUrl && <div style={errorBannerStyle}>{message}</div>}
      </div>
      {imageUrl && (
        <div style={reviewStyle}>
          <span><span style={{ color: OPTICAL_CENTER_COLOR }}>＋</span> Optical image center</span>
          {detectedCenter && <span><span style={{ color: DETECTED_CENTER_COLOR }}>○</span> Detected well center (image evidence)</span>}
          <span>Click the image to propose a review center. This does not move the gantry or save calibration.</span>
          {proposedCenter && offset && (
            <div style={proposalStyle} role="status">
              Proposed center: x {proposedCenter.x.toFixed(3)}, y {proposedCenter.y.toFixed(3)} normalized
              {" · "}offset from optical center {offset.x >= 0 ? "+" : ""}{offset.x.toFixed(1)}% x,
              {" "}{offset.y >= 0 ? "+" : ""}{offset.y.toFixed(1)}% y
              <button type="button" onClick={clearProposal} style={clearButtonStyle}>Clear</button>
            </div>
          )}
        </div>
      )}
    </div>
  );
}

const OPTICAL_CENTER_COLOR = theme.color.danger;
const DETECTED_CENTER_COLOR = theme.color.warning;
const PROPOSED_CENTER_COLOR = theme.color.accent;

const containerStyle: React.CSSProperties = {
  position: "relative",
  width: "100%",
  aspectRatio: "4 / 3",
  background: theme.color.surfaceSunken,
  border: `1px solid ${theme.color.border}`,
  borderRadius: 8,
  overflow: "hidden",
};

const imageStyle: React.CSSProperties = {
  width: "100%",
  height: "100%",
  objectFit: "contain",
  objectPosition: "center center",
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

const reviewStyle: React.CSSProperties = {
  display: "flex",
  flexWrap: "wrap",
  gap: "5px 14px",
  margin: "7px 0 12px",
  color: theme.color.textMuted,
  fontSize: 11,
};

const proposalStyle: React.CSSProperties = {
  flexBasis: "100%",
  padding: "6px 8px",
  border: `1px solid ${theme.color.border}`,
  borderRadius: 6,
  color: theme.color.textSecondary,
};

const clearButtonStyle: React.CSSProperties = {
  marginLeft: 8,
  padding: "2px 6px",
  fontSize: 11,
};
