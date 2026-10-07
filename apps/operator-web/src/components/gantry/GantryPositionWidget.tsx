import { useCallback, useEffect, useRef, useState } from "react";
import { gantryApi } from "../../api/client";
import type { GantryConfig, GantryPosition, GantryResponse, WorkingVolume } from "../../types";
import * as theme from "../../theme";
import CalibrationWizard from "./CalibrationWizard";
import InstrumentOffsetCalibrationModal from "./InstrumentOffsetCalibrationModal";
import { createJogPacer, jogPaceMs } from "./jogPacing";
import { useConfirm } from "../common/useConfirm";

interface Props {
  position: GantryPosition | null;
  workingVolume: WorkingVolume | null;
  gantryFile: string | null;
  gantry: GantryResponse | null;
  isRunning?: boolean;
  onSaveCalibrated: (filename: string, config: GantryConfig) => Promise<void>;
  onSaveInstrumentOffsets?: (filename: string, config: GantryConfig) => Promise<void>;
}

const MIN_STEP = 0.001;

type AxisPosition = {
  x: number;
  y: number;
  z: number;
};

export default function GantryPositionWidget({
  position,
  workingVolume,
  gantryFile,
  gantry,
  isRunning = false,
  onSaveCalibrated,
  onSaveInstrumentOffsets,
}: Props) {
  const [loading, setLoading] = useState(false);
  const [connectionError, setConnectionError] = useState<string | null>(null);
  const [jogBusy, setJogBusy] = useState(false);
  const [homeBusy, setHomeBusy] = useState(false);
  const [calibrationOpen, setCalibrationOpen] = useState(false);
  const [offsetCalibrationOpen, setOffsetCalibrationOpen] = useState(false);
  const anyCalibrationOpen = calibrationOpen || offsetCalibrationOpen;
  const [stepXY, setStepXY] = useState("0.5");
  const [stepZ, setStepZ] = useState("0.5");
  const [moveX, setMoveX] = useState("");
  const [moveY, setMoveY] = useState("");
  const [moveZ, setMoveZ] = useState("");
  const [moveError, setMoveError] = useState<string | null>(null);
  const [lastCommandError, setLastCommandError] = useState<string | null>(null);
  const [limitHint, setLimitHint] = useState<string | null>(null);
  const [savedCalibrationMessage, setSavedCalibrationMessage] = useState<string | null>(null);
  const [advancedOpen, setAdvancedOpen] = useState(false);
  const [advancedBusy, setAdvancedBusy] = useState(false);
  const [advancedMessage, setAdvancedMessage] = useState<string | null>(null);
  const [advancedError, setAdvancedError] = useState<string | null>(null);
  const [restoreBusy, setRestoreBusy] = useState(false);
  const [pullOffBusy, setPullOffBusy] = useState(false);
  const [grblSettings, setGrblSettings] = useState<Record<string, string> | null>(null);
  const [settingKey, setSettingKey] = useState("$20");
  const [settingValue, setSettingValue] = useState("");
  const [requestConfirm, confirmDialog] = useConfirm();
  const jogHeld = useRef<AxisPosition | null>(null);
  const jogPumpActive = useRef(false);
  const jogRequestCount = useRef(0);
  const lastJogDelta = useRef<AxisPosition | null>(null);
  const predictedJogPosition = useRef<AxisPosition | null>(null);
  const limitHintTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  const configSelected = !!gantryFile;
  const connected = configSelected && (position?.connected ?? false);
  const status = position?.status ?? "Not connected";
  const isAlarm = status.toLowerCase().includes("alarm");
  const isLimitAlarm = isAlarm && looksLikeLimitAlarm(status);
  const isHold = status.toLowerCase().startsWith("hold");
  const isMoving = status === "Run" || status === "Jog";
  const calibrationInterrupted = connected && !anyCalibrationOpen && (position?.calibration_active ?? false);

  useEffect(() => {
    if (jogHeld.current || jogPumpActive.current) return;
    predictedJogPosition.current = currentWorkPosition(position);
  }, [position]);

  useEffect(() => {
    if (position?.move_error) {
      setLastCommandError(position.move_error);
    }
  }, [position?.move_error]);

  useEffect(() => () => {
    if (limitHintTimer.current) {
      clearTimeout(limitHintTimer.current);
    }
  }, []);

  const showLimitHint = useCallback(() => {
    setLimitHint("At working-volume limit");
    if (limitHintTimer.current) {
      clearTimeout(limitHintTimer.current);
    }
    limitHintTimer.current = setTimeout(() => setLimitHint(null), 1800);
  }, []);

  const jog = useCallback(async (x: number, y: number, z: number): Promise<boolean> => {
    if (!connected || isRunning || jogBusy || homeBusy) return false;
    if (workingVolume) {
      const base = predictedJogPosition.current ?? currentWorkPosition(position);
      if (base) {
        const target = { x: base.x + x, y: base.y + y, z: base.z + z };
        if (!isInsideWorkingVolume(target, workingVolume)) {
          showLimitHint();
          return false;
        }
        predictedJogPosition.current = target;
      }
    }
    jogRequestCount.current += 1;
    lastJogDelta.current = { x, y, z };
    try {
      await gantryApi.jog(x, y, z);
      setLastCommandError(null);
      return true;
    } catch (e) {
      setLastCommandError(errorMessage(e));
      return false;
    }
  }, [connected, homeBusy, isRunning, jogBusy, position, showLimitHint, workingVolume]);

  // The held-jog pump reads jog through a ref so an in-flight hold always
  // uses the latest guards (connected/busy state) instead of a stale closure.
  const jogRef = useRef(jog);
  useEffect(() => {
    jogRef.current = jog;
  }, [jog]);

  const jogPacer = useRef(createJogPacer()).current;

  const stopJog = useCallback(() => {
    if (jogHeld.current === null) return;
    jogHeld.current = null;
    // A single click lets its full step finish (predictable stepping); a
    // held jog cancels so motion stops at release instead of running out
    // whatever GRBL has queued.
    if (jogRequestCount.current > 1) {
      gantryApi.jogCancel().catch(() => undefined);
    }
    jogRequestCount.current = 0;
    jogPacer.wake();
  }, [jogPacer]);

  const startJog = useCallback((x: number, y: number, z: number) => {
    const heldBefore = jogHeld.current !== null;
    jogHeld.current = { x, y, z };
    if (heldBefore && jogPumpActive.current) {
      // Direction change mid-hold (multi-key / multi-touch): hand the new
      // delta to the pump instead of firing immediately — an immediate send
      // would repeat at the previous segment's pace, and a larger step at a
      // smaller step's cadence re-creates exactly the backlog this pacing
      // exists to prevent. wake() cuts the remaining sleep so the new
      // direction still starts promptly.
      jogPacer.wake();
      return;
    }
    if (!jogPumpActive.current) {
      jogRequestCount.current = 0;
    }
    // The first jog of a distinct press always sends immediately — a click
    // must never wait behind a previous press's pacing.
    const first = jogRef.current(x, y, z);
    if (jogPumpActive.current) return;
    jogPumpActive.current = true;
    const pump = async () => {
      try {
        let sent = { x, y, z };
        let ok = await first;
        while (ok && jogHeld.current) {
          // Pace by the segment actually sent last — the held delta can
          // change mid-hold, and repeats must never outpace the execution
          // time of the segment they follow.
          await jogPacer.sleep(jogPaceMs(sent.x, sent.y, sent.z));
          const delta = jogHeld.current;
          if (!delta) break;
          sent = delta;
          ok = await jogRef.current(delta.x, delta.y, delta.z);
        }
        if (!ok) {
          jogHeld.current = null;
        }
      } finally {
        jogPumpActive.current = false;
      }
    };
    void pump();
  }, [jogPacer]);

  // Clean up on unmount
  useEffect(() => () => stopJog(), [stopJog]);

  useEffect(() => {
    if (anyCalibrationOpen || isRunning) {
      stopJog();
    }
  }, [anyCalibrationOpen, isRunning, stopJog]);

  useEffect(() => {
    if (!connected) {
      setGrblSettings(null);
      setAdvancedMessage(null);
      setAdvancedError(null);
    }
  }, [connected]);

  // Keyboard support: arrow keys for XY, X/Z for Z axis
  useEffect(() => {
    const held = new Set<string>();
    const releaseHeldJog = () => {
      held.clear();
      stopJog();
    };

    const onKeyDown = (e: KeyboardEvent) => {
      if (!connected || anyCalibrationOpen || isRunning || jogBusy || homeBusy) return;
      if (isEditableTarget(e.target)) return;
      const key = e.key;
      if (held.has(key)) return; // already held

      const parsedXY = parsePositiveStep(stepXY);
      const parsedZ = parsePositiveStep(stepZ);
      if (parsedXY == null || parsedZ == null) return;
      held.add(key);

      const xy = Math.max(MIN_STEP, parsedXY);
      const z = Math.max(MIN_STEP, parsedZ);

      switch (key) {
        case "ArrowLeft":  e.preventDefault(); startJog(-xy, 0, 0); break;
        case "ArrowRight": e.preventDefault(); startJog(xy, 0, 0); break;
        case "ArrowUp":    e.preventDefault(); startJog(0, xy, 0); break;
        case "ArrowDown":  e.preventDefault(); startJog(0, -xy, 0); break;
        case "x": case "X": startJog(0, 0, z); break;
        case "z": case "Z": startJog(0, 0, -z); break;
      }
    };

    const onKeyUp = (e: KeyboardEvent) => {
      held.delete(e.key);
      if (["ArrowLeft","ArrowRight","ArrowUp","ArrowDown","z","Z","x","X"].includes(e.key)) {
        stopJog();
      }
    };

    const onVisibilityChange = () => {
      if (document.visibilityState === "hidden") {
        releaseHeldJog();
      }
    };

    window.addEventListener("keydown", onKeyDown);
    window.addEventListener("keyup", onKeyUp);
    window.addEventListener("blur", releaseHeldJog);
    document.addEventListener("visibilitychange", onVisibilityChange);
    return () => {
      window.removeEventListener("keydown", onKeyDown);
      window.removeEventListener("keyup", onKeyUp);
      window.removeEventListener("blur", releaseHeldJog);
      document.removeEventListener("visibilitychange", onVisibilityChange);
    };
  }, [anyCalibrationOpen, connected, homeBusy, isRunning, jogBusy, stepXY, stepZ, startJog, stopJog]);

  const handleConnect = async () => {
    if (!gantryFile) return;
    setLoading(true);
    setConnectionError(null);
    let connectSucceeded = false;
    try {
      await gantryApi.connect(gantryFile);
      connectSucceeded = true;
    } catch (e) {
      setConnectionError(`Connection failed: ${errorMessage(e)}`);
    }
    setLoading(false);
    // A fresh connection usually follows a power cycle, so the controller
    // has no reference position until it homes — offer that right away.
    // Deliberately after the connect request resolves: the prompt's Home
    // fires without handleHome's `connected` guard, since the polled
    // position prop may lag the successful connect by a poll interval.
    if (connectSucceeded && !isRunning) {
      const confirmed = await requestConfirm({
        title: "Gantry connected",
        message: "Home the gantry now? Homing establishes a known reference position and is recommended after connecting.",
        confirmLabel: "Home now",
        cancelLabel: "Not now",
      });
      if (confirmed) await sendHome();
    }
  };

  const handleDisconnect = async () => {
    setLoading(true);
    setConnectionError(null);
    try {
      await gantryApi.disconnect();
    } catch (e) {
      setConnectionError(`Disconnect failed: ${e}`);
    }
    setLoading(false);
  };

  const handleUnlock = async () => {
    if (!connected || isRunning) return;
    setJogBusy(true);
    try {
      await gantryApi.unlock();
      setLastCommandError(null);
    } catch (e) {
      setLastCommandError(errorMessage(e));
    } finally {
      setJogBusy(false);
    }
  };

  const handlePullOff = async () => {
    const delta = lastJogDelta.current;
    if (!connected || !delta || !isLimitAlarm || pullOffBusy || isRunning) return;
    stopJog();
    setPullOffBusy(true);
    try {
      await gantryApi.recoverCalibrationLimit({ x: delta.x, y: delta.y, z: delta.z });
      setLastCommandError(null);
    } catch (e) {
      setLastCommandError(errorMessage(e));
    } finally {
      setPullOffBusy(false);
    }
  };

  const runAdvancedAction = async (label: string, action: () => Promise<void>) => {
    if (!connected || isRunning) return;
    setAdvancedBusy(true);
    setAdvancedMessage(null);
    setAdvancedError(null);
    try {
      await action();
      setAdvancedMessage(label);
    } catch (e) {
      setAdvancedError(e instanceof Error ? e.message : String(e));
    } finally {
      setAdvancedBusy(false);
    }
  };

  const readGrblSettings = () => runAdvancedAction("Read GRBL settings.", async () => {
    const result = await gantryApi.readGrblSettings();
    setGrblSettings(result.settings);
  });

  const applyGrblSetting = () => runAdvancedAction(`Sent ${settingKey}=${settingValue}.`, async () => {
    const result = await gantryApi.setGrblSetting({ setting: settingKey, value: settingValue });
    setGrblSettings(result.settings);
  });

  const resetAndUnlock = () => runAdvancedAction("Reset and unlock sent.", async () => {
    await gantryApi.resetUnlock();
  });

  const clearAlarmAdvanced = () => runAdvancedAction("Unlock sent.", async () => {
    await gantryApi.unlock();
  });

  const restoreInterruptedCalibration = async () => {
    if (!connected || isRunning || restoreBusy) return;
    stopJog();
    setRestoreBusy(true);
    setLastCommandError(null);
    try {
      await gantryApi.restoreCalibrationSoftLimits();
    } catch (e) {
      setLastCommandError(errorMessage(e));
    } finally {
      setRestoreBusy(false);
    }
  };

  const feedHold = () => runAdvancedAction("Feed hold sent.", async () => {
    await gantryApi.feedHold();
  });

  const resume = () => runAdvancedAction("Resume sent; verify Idle before homing.", async () => {
    await gantryApi.resume();
  });

  const cancelJog = () => runAdvancedAction("Jog cancel sent.", async () => {
    await gantryApi.jogCancel();
  });

  const sendHome = async () => {
    setHomeBusy(true);
    try {
      await gantryApi.home();
      setLastCommandError(null);
    } catch (e) {
      setLastCommandError(errorMessage(e));
    } finally {
      setHomeBusy(false);
    }
  };

  const handleHome = async () => {
    if (!connected || isRunning) return;
    const confirmed = await requestConfirm({
      title: "Home gantry",
      message: "Confirm you want to go to home?",
      confirmLabel: "Go to home",
    });
    if (!confirmed) return;
    await sendHome();
  };

  const handleMoveTo = () => {
    if (!connected || isRunning) return;
    setMoveError(null);
    // Blank fields must not silently mean 0 — Number("") is 0, and a
    // half-filled form would command an unintended move on the blank axes.
    const x = parseAxisTarget(moveX);
    const y = parseAxisTarget(moveY);
    const z = parseAxisTarget(moveZ);
    if (x === null || y === null || z === null) {
      setMoveError("Enter valid X, Y, and Z coordinates.");
      return;
    }
    if (workingVolume) {
      const axisChecks: Array<[string, number, number, number]> = [
        ["X", x, workingVolume.x_min, workingVolume.x_max],
        ["Y", y, workingVolume.y_min, workingVolume.y_max],
        ["Z", z, workingVolume.z_min, workingVolume.z_max],
      ];
      const violations = axisChecks.filter(([, value, min, max]) => (
        value < min || value > max
      ));
      if (violations.length > 0) {
        setMoveError(
          `Move target outside working volume: ${violations
            .map(([axis, value, min, max]) => `${axis}=${Number(value).toFixed(3)} outside [${Number(min).toFixed(3)}, ${Number(max).toFixed(3)}]`)
            .join("; ")}`,
        );
        return;
      }
    }
    gantryApi.moveTo(x, y, z)
      .then(() => {
        setMoveError(null);
        setLastCommandError(null);
      })
      .catch((e) => setLastCommandError(errorMessage(e)));
  };

  // 800 steps/mm → min 0.00125mm; clamp to 0.001mm floor
  const parsedXYStep = parsePositiveStep(stepXY);
  const parsedZStep = parsePositiveStep(stepZ);
  const xyStep = parsedXYStep == null ? MIN_STEP : Math.max(MIN_STEP, parsedXYStep);
  const zStep = parsedZStep == null ? MIN_STEP : Math.max(MIN_STEP, parsedZStep);
  const xyBelowMin = parsedXYStep != null && parsedXYStep < MIN_STEP;
  const zBelowMin = parsedZStep != null && parsedZStep < MIN_STEP;
  const stepInvalid = parsedXYStep == null || parsedZStep == null;
  const homeDisabled = !connected || isHold || jogBusy || homeBusy || isRunning;
  const jogDisabled = homeDisabled || stepInvalid;
  const moveDisabled = !connected || isHold || isMoving || isRunning;
  const advancedDisabled = !connected || advancedBusy || isRunning;
  const canCalibrate = !!gantry;
  const canOpenCalibration = canCalibrate && !isRunning;

  const jogBtnProps = (x: number, y: number, z: number) => ({
    onMouseDown: () => !jogDisabled && startJog(x, y, z),
    onMouseUp: stopJog,
    onMouseLeave: stopJog,
    onTouchStart: (e: React.TouchEvent) => { e.preventDefault(); if (!jogDisabled) startJog(x, y, z); },
    onTouchEnd: stopJog,
  });

  const handleSaveCalibrated = useCallback(async (filename: string, config: GantryConfig) => {
    await onSaveCalibrated(filename, config);
    const volume = config.working_volume;
    setSavedCalibrationMessage(
      `Saved ${filename} — X ${formatRange(volume.x_min, volume.x_max)}, Y ${formatRange(volume.y_min, volume.y_max)}, Z ${formatRange(volume.z_min, volume.z_max)} mm`,
    );
    setLastCommandError(null);
  }, [onSaveCalibrated]);

  // Status color and label
  const statusColor = isAlarm ? theme.color.danger : status === "Idle" ? theme.color.success : status === "Run" || status === "Jog" ? theme.color.accent : theme.color.textMuted;

  return (
    <div>
      <div style={controlHeaderStyle}>
        <div>
          <h3 style={theme.panelTitle}>Gantry Control</h3>
          <div style={{ ...theme.mono, fontSize: 11, color: theme.color.textMuted, marginTop: 2 }}>{gantryFile ?? "No gantry config loaded"}</div>
        </div>
        <button
          onClick={() => setAdvancedOpen((open) => !open)}
          style={{
            ...advancedToggleStyle,
            background: advancedOpen ? theme.color.accentTint : theme.color.surface,
            color: advancedOpen ? theme.color.accentText : theme.color.textSecondary,
            border: advancedOpen
              ? `1px solid ${theme.color.accentTintBorder}`
              : `1px solid ${theme.color.borderStrong}`,
          }}
          aria-pressed={advancedOpen}
        >
          Advanced
        </button>
      </div>

      {/* Alarm banner */}
      {isAlarm && connected && (
        <div style={{
          ...theme.notice.error,
          marginBottom: 12,
          display: "flex",
          alignItems: "center",
          gap: 8,
        }}>
          <span style={{ color: theme.color.danger, fontWeight: 700, fontSize: 13 }}>ALARM</span>
          <span style={{ color: theme.color.dangerText, fontSize: 11 }}>
            {status} — {isLimitAlarm && lastJogDelta.current
              ? "Pull off backs away from the limit switch automatically, or Unlock to clear and jog back manually."
              : "Unlock to clear, then jog back to safety."}
          </span>
          {isLimitAlarm && lastJogDelta.current && (
            <button
              onClick={handlePullOff}
              disabled={pullOffBusy || jogBusy || isRunning}
              style={{
                ...theme.btn.danger,
                ...theme.btnSmall,
                marginLeft: "auto",
              }}
            >
              {pullOffBusy ? "Pulling off..." : "Pull off limit"}
            </button>
          )}
          <button
            onClick={handleUnlock}
            disabled={jogBusy || pullOffBusy || isRunning}
            style={{
              ...theme.btn.danger,
              ...theme.btnSmall,
              marginLeft: isLimitAlarm && lastJogDelta.current ? undefined : "auto",
            }}
          >
            Unlock ($X)
          </button>
        </div>
      )}

      {isHold && connected && (
        <div style={{
          ...theme.notice.warning,
          marginBottom: 12,
          display: "flex",
          alignItems: "center",
          gap: 8,
        }}>
          <span style={{ color: theme.color.warningText, fontWeight: 700, fontSize: 13 }}>HOLD</span>
          <span style={{ color: theme.color.warningText, fontSize: 11 }}>
            Feed hold is active — Home and manual motion are blocked. Resume only when the motion path is clear.
          </span>
          <button
            onClick={resume}
            disabled={advancedDisabled}
            style={{
              ...theme.btn.secondary,
              ...theme.btnSmall,
              marginLeft: "auto",
            }}
          >
            Resume
          </button>
        </div>
      )}


      {calibrationInterrupted && (
        <div style={interruptedCalibrationStyle}>
          <span style={{ color: theme.color.danger, fontWeight: 700, fontSize: 13 }}>CALIBRATION INTERRUPTED</span>
          <span style={{ color: theme.color.dangerText, fontSize: 11 }}>
            Calibration interrupted — soft limits are disabled
          </span>
          <button
            onClick={restoreInterruptedCalibration}
            disabled={restoreBusy || isRunning}
            style={buttonStateStyle(interruptedCalibrationButtonStyle, restoreBusy || isRunning)}
          >
            {restoreBusy ? "Restoring..." : "Restore soft limits"}
          </button>
        </div>
      )}

      {isRunning && (
        <div style={runLockStyle}>Protocol running — manual control locked</div>
      )}

      {savedCalibrationMessage && (
        <div style={successStyle}>{savedCalibrationMessage}</div>
      )}

      {lastCommandError && (
        <div role="alert" style={commandErrorStyle}>
          <span>{lastCommandError}</span>
          <button
            type="button"
            aria-label="Dismiss command error"
            onClick={() => setLastCommandError(null)}
            style={dismissErrorButtonStyle}
          >
            ×
          </button>
        </div>
      )}

      {limitHint && <div style={limitHintStyle}>{limitHint}</div>}

      {/* Top row: D-pad + Z on left, XYZ readout on right */}
      <div style={{ display: "flex", gap: 24, marginBottom: 12 }}>
        {/* Jog controls */}
        <div>
          <div style={{ display: "flex", gap: 24, alignItems: "center", marginBottom: 8 }}>
            {/* XY D-pad */}
            <div style={{ display: "grid", gridTemplateColumns: "repeat(3, 40px)", gridTemplateRows: "repeat(3, 40px)", gap: 2 }}>
              <div />
              <button className="jog-btn" style={jogBtnStyle} disabled={jogDisabled} {...jogBtnProps(0, xyStep, 0)} title="Y+">
                ↑
              </button>
              <div />
              <button className="jog-btn" style={jogBtnStyle} disabled={jogDisabled} {...jogBtnProps(-xyStep, 0, 0)} title="X-">
                ←
              </button>
              <div style={{ display: "flex", alignItems: "center", justifyContent: "center", ...theme.sectionLabel, fontSize: 10 }}>
                XY
              </div>
              <button className="jog-btn" style={jogBtnStyle} disabled={jogDisabled} {...jogBtnProps(xyStep, 0, 0)} title="X+">
                →
              </button>
              <div />
              <button className="jog-btn" style={jogBtnStyle} disabled={jogDisabled} {...jogBtnProps(0, -xyStep, 0)} title="Y-">
                ↓
              </button>
              <div />
            </div>

            {/* Z controls */}
            <div style={{ display: "flex", flexDirection: "column", gap: 2 }}>
              <button className="jog-btn" style={jogBtnStyle} disabled={jogDisabled} {...jogBtnProps(0, 0, zStep)} title="Z+">
                Z+
              </button>
              <div style={{ ...theme.sectionLabel, fontSize: 10, textAlign: "center" }}>Z</div>
              <button className="jog-btn" style={jogBtnStyle} disabled={jogDisabled} {...jogBtnProps(0, 0, -zStep)} title="Z-">
                Z−
              </button>
            </div>
          </div>

          {/* Step size inputs */}
          <div style={{ display: "flex", gap: 12, fontSize: 11 }}>
            <label style={{ display: "flex", alignItems: "center", gap: 4 }}>
              <span style={{ color: theme.color.textMuted }}>XY mm</span>
              <input
                type="text"
                inputMode="decimal"
                value={stepXY}
                onChange={(e) => setStepXY(e.target.value)}
                style={{ ...inputStyle, ...theme.mono, width: 48, fontSize: 11, padding: "2px 4px", borderColor: parsedXYStep == null || xyBelowMin ? theme.color.danger : theme.color.borderStrong }}
              />
            </label>
            <label style={{ display: "flex", alignItems: "center", gap: 4 }}>
              <span style={{ color: theme.color.textMuted }}>Z mm</span>
              <input
                type="text"
                inputMode="decimal"
                value={stepZ}
                onChange={(e) => setStepZ(e.target.value)}
                style={{ ...inputStyle, ...theme.mono, width: 48, fontSize: 11, padding: "2px 4px", borderColor: parsedZStep == null || zBelowMin ? theme.color.danger : theme.color.borderStrong }}
              />
            </label>
            {(stepInvalid || xyBelowMin || zBelowMin) && (
              <span style={{ color: theme.color.danger, fontSize: 10, alignSelf: "center" }}>
                {stepInvalid ? "Enter step sizes greater than 0" : `min ${MIN_STEP}mm`}
              </span>
            )}
          </div>
        </div>

        {/* XYZ Readout */}
        <div style={{
          display: "flex",
          flexDirection: "column",
          justifyContent: "center",
          gap: 6,
          background: theme.color.surfaceSunken,
          border: `1px solid ${theme.color.border}`,
          borderRadius: theme.radius.md,
          padding: "10px 16px",
        }}>
          {(["X", "Y", "Z"] as const).map((axis) => {
            const rawMpos = connected ? position![axis.toLowerCase() as "x" | "y" | "z"] : null;
            const mpos = rawMpos;
            const wKey = `work_${axis.toLowerCase()}` as "work_x" | "work_y" | "work_z";
            const rawWpos = connected ? position![wKey] : null;
            const wpos = rawWpos;
            const hasPosition = wpos != null || mpos != null;
            return (
              <div key={axis} style={{ display: "flex", alignItems: "baseline", gap: 6 }}>
                <span style={{ ...theme.sectionLabel, width: 14 }}>{axis}</span>
                <span style={hasPosition ? liveCoordStyle : coordPlaceholderStyle}>
                  {wpos != null ? wpos.toFixed(3) : mpos != null ? mpos.toFixed(3) : "\u2014"}
                </span>
                {wpos != null && mpos != null && (
                  <span style={{ ...theme.mono, color: theme.color.textFaint, fontSize: 10 }}>M{mpos.toFixed(1)}</span>
                )}
              </div>
            );
          })}
          <div style={{
            fontSize: 12,
            color: statusColor,
            fontWeight: isAlarm ? 700 : 500,
            marginTop: 2,
          }}>
            {status}
          </div>
        </div>
      </div>

      {/* Home and calibration */}
      <div style={{ marginBottom: 10, display: "flex", gap: 8, flexWrap: "wrap" }}>
        <button onClick={handleHome} disabled={homeDisabled} style={buttonStateStyle(homeBtnStyle, homeDisabled)}>
          {homeBusy ? "Homing…" : "Home"}
        </button>
        <button
          onClick={() => setCalibrationOpen(true)}
          disabled={!canOpenCalibration}
          style={{
            ...calibrateBtnStyle,
            opacity: canOpenCalibration ? 1 : 0.45,
            cursor: canOpenCalibration ? "pointer" : "not-allowed",
          }}
          title={canOpenCalibration ? "Open gantry calibration" : isRunning ? "Protocol running" : "Load a gantry config first"}
        >
          Calibrate
        </button>
        <button onClick={() => setOffsetCalibrationOpen(true)} disabled={!onSaveInstrumentOffsets || !canOpenCalibration || !connected || calibrationInterrupted || homeBusy || jogBusy}
          style={calibrateBtnStyle}>Calibrate instrument offsets</button>
      </div>

      {workingVolume && (
        <div style={{ ...theme.mono, fontSize: 10, color: theme.color.textFaint, marginBottom: 8 }}>
          Vol: X[{workingVolume.x_min}, {workingVolume.x_max}] Y[{workingVolume.y_min},{" "}
          {workingVolume.y_max}] Z[{workingVolume.z_min}, {workingVolume.z_max}]
        </div>
      )}

      {/* Move To */}
      {connected && (
        <div style={{ marginBottom: 10, borderTop: `1px solid ${theme.color.border}`, paddingTop: 10 }}>
          <div style={{ ...theme.sectionLabel, marginBottom: 6 }}>Move To</div>
          <div style={{ display: "flex", gap: 6, alignItems: "center" }}>
            {(["X", "Y", "Z"] as const).map((axis) => {
              const setter = axis === "X" ? setMoveX : axis === "Y" ? setMoveY : setMoveZ;
              const value = axis === "X" ? moveX : axis === "Y" ? moveY : moveZ;
              return (
                <label key={axis} style={{ display: "flex", alignItems: "center", gap: 2, fontSize: 11 }}>
                  <span style={{ color: theme.color.textMuted }}>{axis} (mm)</span>
                  <input
                    type="text"
                    inputMode="decimal"
                    value={value}
                    onChange={(e) => setter(e.target.value)}
                    placeholder={workingVolume ? axisRangePlaceholder(axis, workingVolume) : "mm"}
                    disabled={moveDisabled}
                    style={buttonStateStyle({ ...inputStyle, ...theme.mono, width: 70, fontSize: 11, padding: "3px 4px" }, moveDisabled)}
                  />
                </label>
              );
            })}
            <button
              onClick={handleMoveTo}
              disabled={moveDisabled}
              style={{
                ...theme.btn.primary,
                ...theme.btnSmall,
                opacity: moveDisabled ? 0.6 : 1,
              }}
            >
              {isMoving ? "Moving..." : "Go"}
            </button>
          </div>
          {moveError && <div style={moveErrorStyle}>{moveError}</div>}
        </div>
      )}

      {/* Connection controls — bottom */}
      <div style={{ display: "flex", gap: 8, alignItems: "center", borderTop: `1px solid ${theme.color.border}`, paddingTop: 10 }}>
        <span style={{
          width: 8, height: 8, borderRadius: "50%",
          background: isAlarm ? theme.color.danger : connected ? theme.color.success : theme.color.textFaint,
          display: "inline-block",
          flexShrink: 0,
        }} />
        <span style={{ fontSize: 11, color: isAlarm ? theme.color.danger : connected ? theme.color.successText : theme.color.textMuted }}>
          {connected ? (isAlarm ? "Alarm" : "Connected") : "Not connected"}
        </span>
        {!connected ? (
          <button onClick={handleConnect} disabled={loading || !configSelected} style={buttonStateStyle(btnStyle, loading || !configSelected)}>
            {!configSelected ? "Select config first" : loading ? "Connecting..." : "Connect"}
          </button>
        ) : (
          <button onClick={handleDisconnect} disabled={loading} style={buttonStateStyle(btnStyle, loading)}>
            {loading ? "Disconnecting..." : "Disconnect"}
          </button>
        )}
        {connectionError && (
          <span style={{ color: theme.color.dangerText, fontSize: 11, marginLeft: 8 }}>{connectionError}</span>
        )}
      </div>

      {advancedOpen && (
        <div style={advancedPanelStyle}>
          <div style={advancedGridStyle}>
            <button onClick={readGrblSettings} disabled={advancedDisabled} style={buttonStateStyle(btnStyle, advancedDisabled)}>
              Read GRBL Settings
            </button>
            <button onClick={clearAlarmAdvanced} disabled={advancedDisabled} style={buttonStateStyle(btnStyle, advancedDisabled)}>
              Clear Alarm
            </button>
            <button onClick={resetAndUnlock} disabled={advancedDisabled} style={buttonStateStyle(warnBtnStyle, advancedDisabled)}>
              Reset + Unlock
            </button>
            <button onClick={feedHold} disabled={advancedDisabled} style={buttonStateStyle(warnBtnStyle, advancedDisabled)}>
              Feed Hold
            </button>
            <button onClick={resume} disabled={advancedDisabled} style={buttonStateStyle(btnStyle, advancedDisabled)}>
              Resume
            </button>
            <button onClick={cancelJog} disabled={advancedDisabled} style={buttonStateStyle(btnStyle, advancedDisabled)}>
              Cancel Jog
            </button>
          </div>
          <div style={settingRowStyle}>
            <label style={settingFieldStyle}>
              <span style={advancedLabelStyle}>Setting</span>
              <input
                value={settingKey}
                onChange={(event) => setSettingKey(event.target.value)}
                disabled={advancedDisabled}
                style={inputStyle}
                placeholder="$20"
              />
            </label>
            <label style={settingFieldStyle}>
              <span style={advancedLabelStyle}>Value</span>
              <input
                value={settingValue}
                onChange={(event) => setSettingValue(event.target.value)}
                disabled={advancedDisabled}
                style={inputStyle}
                placeholder="0"
              />
            </label>
            <button onClick={applyGrblSetting} disabled={advancedDisabled || !settingKey.trim() || !settingValue.trim()} style={buttonStateStyle(primarySmallBtnStyle, advancedDisabled || !settingKey.trim() || !settingValue.trim())}>
              Send Setting
            </button>
          </div>
          {advancedMessage && <div style={advancedMessageStyle}>{advancedMessage}</div>}
          {advancedError && <div style={advancedErrorStyle}>{advancedError}</div>}
          {grblSettings && (
            <div style={settingsTableStyle}>
              {Object.entries(grblSettings)
                .sort(([left], [right]) => Number(left.replace("$", "")) - Number(right.replace("$", "")))
                .map(([key, value]) => (
                  <div key={key} style={settingsRowStyle}>
                    <span style={settingsKeyStyle}>{key}</span>
                    <span style={settingsValueStyle}>{value}</span>
                  </div>
                ))}
            </div>
          )}
        </div>
      )}

      <div style={{ fontSize: 10, color: theme.color.textFaint, marginTop: 8 }}>
        Keyboard: Arrow keys = XY, X/Z keys = Z up/down
      </div>
      {confirmDialog}
      <InstrumentOffsetCalibrationModal
        open={offsetCalibrationOpen}
        onClose={() => setOffsetCalibrationOpen(false)}
        gantry={gantry}
        position={position}
        isRunning={isRunning}
        onSaveCalibrated={async (filename, config) => {
          if (!onSaveInstrumentOffsets) throw new Error("Offset saving is unavailable.");
          await onSaveInstrumentOffsets(filename, config);
          setSavedCalibrationMessage(`Saved ${filename}. The current connection is unchanged. Select the saved configuration and reconnect explicitly before using its offsets.`);
          setLastCommandError(null);
        }}
      />
      <CalibrationWizard
        open={calibrationOpen}
        onClose={() => setCalibrationOpen(false)}
        gantry={gantry}
        position={position}
        onSaveCalibrated={handleSaveCalibrated}
      />
    </div>
  );
}

function currentWorkPosition(position: GantryPosition | null): AxisPosition | null {
  if (!position?.connected) return null;
  const coords = {
    x: position.work_x ?? position.x,
    y: position.work_y ?? position.y,
    z: position.work_z ?? position.z,
  };
  if (!Number.isFinite(coords.x) || !Number.isFinite(coords.y) || !Number.isFinite(coords.z)) {
    return null;
  }
  return coords;
}

function isInsideWorkingVolume(position: AxisPosition, volume: WorkingVolume): boolean {
  return (
    position.x >= volume.x_min
    && position.x <= volume.x_max
    && position.y >= volume.y_min
    && position.y <= volume.y_max
    && position.z >= volume.z_min
    && position.z <= volume.z_max
  );
}

function parsePositiveStep(value: string): number | null {
  const parsed = Number(value);
  return Number.isFinite(parsed) && parsed > 0 ? parsed : null;
}

function parseAxisTarget(value: string): number | null {
  const trimmed = value.trim();
  if (trimmed === "") return null;
  const parsed = Number(trimmed);
  return Number.isFinite(parsed) ? parsed : null;
}

// Only GRBL limit trips warrant an automatic pull-off move: ALARM:1 (hard
// limit) and ALARM:2 (soft limit), or a status carrying "limit" / an active
// limit pin report ("Pn:"). Other alarms — abort during cycle (E-stop /
// reset, ALARM:3), probe failures, homing failures — do not mean the gantry
// is sitting on a switch, and blindly jogging 5 mm in response to them is
// the wrong reflex.
function looksLikeLimitAlarm(status: string): boolean {
  const lower = status.toLowerCase();
  return /alarm:\s*[12]\b/.test(lower) || lower.includes("limit") || lower.includes("pn:");
}

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

function isEditableTarget(target: EventTarget | null): boolean {
  if (!(target instanceof HTMLElement)) return false;
  if (target.isContentEditable) return true;
  return !!target.closest("input, select, textarea, button, [contenteditable='true']");
}

function formatRange(min: number, max: number): string {
  return `${formatMm(min)}–${formatMm(max)}`;
}

function formatMm(value: number): string {
  return Number.isInteger(value) ? String(value) : value.toFixed(3).replace(/0+$/, "").replace(/\.$/, "");
}

function axisRangePlaceholder(axis: "X" | "Y" | "Z", volume: WorkingVolume): string {
  if (axis === "X") return formatRange(volume.x_min, volume.x_max);
  if (axis === "Y") return formatRange(volume.y_min, volume.y_max);
  return formatRange(volume.z_min, volume.z_max);
}

const coordStyle: React.CSSProperties = {
  ...theme.mono,
  fontSize: 26,
  fontWeight: 600,
  minWidth: 100,
  textAlign: "right",
  display: "inline-block",
  color: theme.color.ink,
  letterSpacing: "0.01em",
};

const liveCoordStyle: React.CSSProperties = {
  ...coordStyle,
  color: theme.color.accentText,
  textShadow: theme.chrome.telemetryGlow,
};

const coordPlaceholderStyle: React.CSSProperties = {
  ...coordStyle,
  color: theme.color.textFaint,
};

function buttonStateStyle(base: React.CSSProperties, disabled: boolean): React.CSSProperties {
  if (!disabled) return base;
  return {
    ...base,
    opacity: 0.45,
    cursor: "not-allowed",
  };
}

const controlHeaderStyle: React.CSSProperties = {
  display: "flex",
  justifyContent: "space-between",
  alignItems: "flex-start",
  gap: 12,
  marginBottom: 12,
};

const advancedToggleStyle: React.CSSProperties = {
  ...theme.btn.secondary,
  ...theme.btnSmall,
  fontWeight: 600,
};

const btnStyle: React.CSSProperties = {
  ...theme.btn.secondary,
  ...theme.btnSmall,
};

const primarySmallBtnStyle: React.CSSProperties = {
  ...theme.btn.primary,
  ...theme.btnSmall,
};

const warnBtnStyle: React.CSSProperties = {
  ...theme.btn.secondary,
  ...theme.btnSmall,
  color: theme.color.warningText,
  border: `1px solid ${theme.color.warningBorder}`,
  background: theme.color.warningBg,
  fontWeight: 600,
};

const inputStyle: React.CSSProperties = {
  ...theme.input,
  padding: "4px 8px",
  fontSize: 12,
};

const advancedPanelStyle: React.CSSProperties = {
  borderTop: `1px solid ${theme.color.border}`,
  marginTop: 10,
  paddingTop: 10,
  display: "grid",
  gap: 8,
};

const advancedGridStyle: React.CSSProperties = {
  display: "flex",
  flexWrap: "wrap",
  gap: 6,
};

const settingRowStyle: React.CSSProperties = {
  display: "grid",
  gridTemplateColumns: "minmax(68px, 92px) minmax(80px, 1fr) auto",
  gap: 6,
  alignItems: "end",
};

const settingFieldStyle: React.CSSProperties = {
  display: "grid",
  gap: 3,
  minWidth: 0,
};

const advancedLabelStyle: React.CSSProperties = {
  ...theme.sectionLabel,
  fontSize: 10,
};

const advancedMessageStyle: React.CSSProperties = {
  color: theme.color.successText,
  fontSize: 11,
};

const advancedErrorStyle: React.CSSProperties = {
  color: theme.color.dangerText,
  fontSize: 11,
};

const runLockStyle: React.CSSProperties = {
  ...theme.notice.warning,
  fontWeight: 600,
  marginBottom: 10,
};

const interruptedCalibrationStyle: React.CSSProperties = {
  ...theme.notice.error,
  marginBottom: 12,
  display: "flex",
  alignItems: "center",
  gap: 8,
  flexWrap: "wrap",
};

const interruptedCalibrationButtonStyle: React.CSSProperties = {
  ...theme.btn.danger,
  ...theme.btnSmall,
  marginLeft: "auto",
};

const successStyle: React.CSSProperties = {
  ...theme.notice.success,
  marginBottom: 10,
};

const commandErrorStyle: React.CSSProperties = {
  ...theme.notice.error,
  marginBottom: 10,
  display: "flex",
  alignItems: "center",
  justifyContent: "space-between",
  gap: 8,
};

const dismissErrorButtonStyle: React.CSSProperties = {
  border: `1px solid ${theme.color.dangerBorder}`,
  background: theme.color.surface,
  color: theme.color.dangerText,
  borderRadius: theme.radius.sm,
  cursor: "pointer",
  width: 22,
  height: 22,
  lineHeight: 1,
  flexShrink: 0,
};

const limitHintStyle: React.CSSProperties = {
  ...theme.notice.warning,
  padding: "5px 8px",
  fontSize: 11,
  marginBottom: 10,
};

const moveErrorStyle: React.CSSProperties = {
  color: theme.color.dangerText,
  fontSize: 11,
  marginTop: 6,
};

const settingsTableStyle: React.CSSProperties = {
  border: `1px solid ${theme.color.border}`,
  borderRadius: theme.radius.sm,
  background: theme.color.surfaceMuted,
  maxHeight: 150,
  overflow: "auto",
  display: "grid",
  gridTemplateColumns: "repeat(auto-fit, minmax(92px, 1fr))",
};

const settingsRowStyle: React.CSSProperties = {
  display: "flex",
  justifyContent: "space-between",
  gap: 8,
  padding: "4px 7px",
  borderBottom: `1px solid ${theme.color.border}`,
  fontSize: 11,
};

const settingsKeyStyle: React.CSSProperties = {
  ...theme.mono,
  color: theme.color.textSecondary,
  fontWeight: 600,
};

const settingsValueStyle: React.CSSProperties = {
  ...theme.mono,
  color: theme.color.ink,
};

const homeBtnStyle: React.CSSProperties = {
  ...theme.btn.secondary,
  ...theme.btnSmall,
  color: theme.color.warningText,
  border: `1px solid ${theme.color.warningBorder}`,
  background: theme.color.warningBg,
  padding: "5px 16px",
  fontWeight: 600,
};

const calibrateBtnStyle: React.CSSProperties = {
  ...theme.btn.primary,
  ...theme.btnSmall,
  padding: "5px 16px",
};

const jogBtnStyle: React.CSSProperties = {
  width: 40,
  height: 40,
  display: "flex",
  alignItems: "center",
  justifyContent: "center",
  background: theme.color.surface,
  border: `1px solid ${theme.color.borderStrong}`,
  borderRadius: theme.radius.md,
  cursor: "pointer",
  fontSize: 16,
  fontWeight: 600,
  color: theme.color.ink,
  transition: "background 0.1s, transform 0.1s",
};
