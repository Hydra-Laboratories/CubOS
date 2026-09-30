import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { KeyboardEvent as ReactKeyboardEvent, TouchEvent } from "react";
import { gantryApi } from "../../api/client";
import * as theme from "../../theme";
import CameraPreview from "./CameraPreview";
import type { GantryConfig, GantryPosition, GantryResponse } from "../../types";
import {
  buildCalibratedConfig,
  calculateSingleInstrumentZCalibration,
  getConfiguredHomingPullOff,
  getFactoryZTravel,
  type ZCalibrationResult,
} from "./calibrationMath";
import { createJogPacer, jogPaceMs } from "./jogPacing";

interface Props {
  open: boolean;
  onClose: () => void;
  gantry: GantryResponse | null;
  position: GantryPosition | null;
  onSaveCalibrated: (filename: string, config: GantryConfig) => Promise<void>;
}

type CapturedPosition = {
  x: number;
  y: number;
  z: number;
};

type JogDelta = {
  x: number;
  y: number;
  z: number;
};

type PendingSingleOrigin = {
  blockTouch: CapturedPosition;
  zReference: CapturedPosition;
};

type PendingZReference = {
  blockTouch: CapturedPosition;
  zReference: CapturedPosition;
  calibration: ZCalibrationResult;
  lowestInstrument: string;
};

const MIN_STEP = 0.001;
const NON_CONTACT_TYPES = new Set(["camera"]);
const FOLLOW_CAMERA_TYPES = new Set(["lighting"]);

function isNonContactInstrument(type: string | undefined): boolean {
  return type != null && NON_CONTACT_TYPES.has(type);
}

function isFollowCameraInstrument(type: string | undefined): boolean {
  return type != null && FOLLOW_CAMERA_TYPES.has(type);
}

export default function CalibrationWizard({
  open,
  onClose,
  gantry,
  position,
  onSaveCalibrated,
}: Props) {
  const [step, setStep] = useState(0);
  const [xyStep, setXyStep] = useState("0.5");
  const [zStep, setZStep] = useState("0.5");
  const [blockHeight, setBlockHeight] = useState("10");
  const [busy, setBusy] = useState(false);
  const [operation, setOperation] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [alarmPrompt, setAlarmPrompt] = useState<string | null>(null);
  const [statusNote, setStatusNote] = useState<string | null>(null);
  const [xyOrigin, setXyOrigin] = useState<CapturedPosition | null>(null);
  const [zReference, setZReference] = useState<CapturedPosition | null>(null);
  const [blockTouch, setBlockTouch] = useState<CapturedPosition | null>(null);
  const [calibrationHome, setCalibrationHome] = useState<CapturedPosition | null>(null);
  const [zCalibration, setZCalibration] = useState<ZCalibrationResult | null>(null);
  const [xyBounds, setXyBounds] = useState<CapturedPosition | null>(null);
  const [instrumentPositions, setInstrumentPositions] = useState<Record<string, CapturedPosition>>({});
  const [cameraBlockDistances, setCameraBlockDistances] = useState<Record<string, string>>({});
  const [tipAttached, setTipAttached] = useState<Record<string, boolean>>({});
  const [tipLengths, setTipLengths] = useState<Record<string, string>>({});
  const [outputFile, setOutputFile] = useState("");
  const [referenceInstrument, setReferenceInstrument] = useState("");
  const [lowestInstrument, setLowestInstrument] = useState("");
  const [resolvedAlarmStatus, setResolvedAlarmStatus] = useState<string | null>(null);
  const jogHeld = useRef<JogDelta | null>(null);
  const jogPumpActive = useRef(false);
  const jogRequestCount = useRef(0);
  const dialogRef = useRef<HTMLDivElement | null>(null);
  const previousFocusRef = useRef<HTMLElement | null>(null);
  const recoveryInProgress = useRef(false);
  const lastJogDelta = useRef<JogDelta | null>(null);
  const recoveryAttemptKey = useRef<string | null>(null);
  const previousOpen = useRef(false);
  const previousStep = useRef(step);
  const pendingSingleOrigin = useRef<PendingSingleOrigin | null>(null);
  const pendingZReference = useRef<PendingZReference | null>(null);

  const filename = gantry?.filename ?? "";
  const config = gantry?.config ?? null;
  const originPolicy = config?.origin_policy ?? "deck_origin";
  const instruments = useMemo(() => Object.keys(config?.instruments ?? {}), [config]);
  const followCameraInstruments = useMemo(
    () => instruments.filter((name) => isFollowCameraInstrument(config?.instruments[name]?.type)),
    [config, instruments],
  );
  const calibratableInstruments = useMemo(
    () => instruments.filter((name) => !followCameraInstruments.includes(name)),
    [instruments, followCameraInstruments],
  );
  const nonContactInstruments = useMemo(
    () => calibratableInstruments.filter((name) => isNonContactInstrument(config?.instruments[name]?.type)),
    [config, calibratableInstruments],
  );
  const contactInstruments = useMemo(
    () => calibratableInstruments.filter((name) => !nonContactInstruments.includes(name)),
    [calibratableInstruments, nonContactInstruments],
  );
  const isMulti = calibratableInstruments.length > 1;
  const connected = position?.connected ?? false;
  const status = position?.status ?? "";
  const rawIsAlarm = looksLikeAlarm(status);
  const isAlarm = rawIsAlarm && status !== resolvedAlarmStatus;
  const alarmRecoveryMessage = alarmPrompt ?? (
    isAlarm
      ? `${status} - controls are locked until limit recovery or manual reset clears the controller.`
      : null
  );
  const normalizedOutput = outputFile.trim() || defaultOutputFilename(filename);
  const selectedReference = referenceInstrument || contactInstruments[0] || "";
  const selectedLowest = lowestInstrument || contactInstruments[0] || "";
  const instrumentSequence = useMemo(
    () => unique([selectedReference, ...calibratableInstruments]).filter((name) => name && name !== selectedLowest),
    [calibratableInstruments, selectedLowest, selectedReference],
  );
  const nextInstrumentToRecord = instrumentSequence.find((name) => !instrumentPositions[name]) ?? null;
  const nextInstrumentIsCamera = nextInstrumentToRecord ? nonContactInstruments.includes(nextInstrumentToRecord) : false;
  const nextCameraDistanceError = nextInstrumentIsCamera && nextInstrumentToRecord
    ? validateCameraBlockDistance(cameraBlockDistances[nextInstrumentToRecord])
    : null;
  const nextInstrumentIsPipette = nextInstrumentToRecord
    ? config?.instruments[nextInstrumentToRecord]?.type === "pipette"
    : false;
  const nextInstrumentTipAttached = nextInstrumentToRecord ? !!tipAttached[nextInstrumentToRecord] : false;
  const nextTipLengthError = nextInstrumentTipAttached && nextInstrumentToRecord
    ? validateTipLength(tipLengths[nextInstrumentToRecord])
    : null;
  // The lowest/reference instrument's block touch feeds directly into the Z
  // working-volume bounds (see calculateSingleInstrumentZCalibration) for
  // both flows, so it needs the same tip-attached compensation as any other
  // pipette touch, not just the non-reference instruments recorded above.
  const lowestInstrumentIsPipette = selectedLowest
    ? config?.instruments[selectedLowest]?.type === "pipette"
    : false;
  const lowestTipAttached = lowestInstrumentIsPipette && !!tipAttached[selectedLowest];
  const lowestTipLengthError = lowestTipAttached ? validateTipLength(tipLengths[selectedLowest]) : null;
  const soleInstrument = !isMulti ? (instruments[0] ?? "") : "";
  const soleInstrumentIsPipette = soleInstrument
    ? config?.instruments[soleInstrument]?.type === "pipette"
    : false;
  const soleTipAttached = soleInstrumentIsPipette && !!tipAttached[soleInstrument];
  const soleTipLengthError = soleTipAttached ? validateTipLength(tipLengths[soleInstrument]) : null;
  const readyForSave = isMulti
    ? !!zReference && !!zCalibration && allInstrumentPositionsReady(calibratableInstruments, instrumentPositions, selectedReference, selectedLowest)
    : !!zReference && !!blockTouch && !!calibrationHome;
  const controlsLocked = busy || !!alarmRecoveryMessage;

  useEffect(() => {
    const wasOpen = previousOpen.current;
    previousOpen.current = open;
    if (!open || wasOpen) return;
    setStep(0);
    setBusy(false);
    setOperation(null);
    setError(null);
    setAlarmPrompt(null);
    setStatusNote(null);
    setXyOrigin(null);
    setZReference(null);
    setBlockTouch(null);
    setCalibrationHome(null);
    setZCalibration(null);
    setXyBounds(null);
    setInstrumentPositions({});
    setCameraBlockDistances({});
    setOutputFile(defaultOutputFilename(filename));
    setReferenceInstrument(contactInstruments[0] ?? "");
    setLowestInstrument(contactInstruments[0] ?? "");
    setBlockHeight(formatOptionalNumber(config?.cnc.calibration_block_height_mm));
    setResolvedAlarmStatus(null);
    lastJogDelta.current = null;
    jogRequestCount.current = 0;
    recoveryAttemptKey.current = null;
    pendingSingleOrigin.current = null;
    pendingZReference.current = null;
  }, [config?.cnc.calibration_block_height_mm, contactInstruments, filename, open]);

  useEffect(() => {
    const priorStep = previousStep.current;
    previousStep.current = step;
    if (priorStep === step) return;
    if (step !== 3) {
      pendingSingleOrigin.current = null;
    }
    if (step !== 4) {
      pendingZReference.current = null;
    }
  }, [step]);

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

  const rememberJogDelta = useCallback((delta: JogDelta) => {
    if (!isZeroDelta(delta)) {
      lastJogDelta.current = delta;
    }
  }, []);

  useEffect(() => {
    if (!rawIsAlarm && resolvedAlarmStatus !== null) {
      setResolvedAlarmStatus(null);
      recoveryAttemptKey.current = null;
    }
  }, [rawIsAlarm, resolvedAlarmStatus]);

  useEffect(() => {
    if (resolvedAlarmStatus === null) return;
    const timer = window.setTimeout(() => {
      setResolvedAlarmStatus(null);
      recoveryAttemptKey.current = null;
    }, 1000);
    return () => window.clearTimeout(timer);
  }, [resolvedAlarmStatus]);

  const recoverFromLimitAlarm = useCallback(async (delta: JogDelta, err: unknown, resolvedStatus?: string) => {
    if (recoveryInProgress.current) return;
    recoveryInProgress.current = true;
    stopJog();
    const message = err instanceof Error ? err.message : String(err);
    setBusy(true);
    setOperation("Recovering from limit switch");
    setError(null);
    setAlarmPrompt(
      "The gantry hit a limit switch. Wait while it backs off.",
    );
    setStatusNote(null);
    try {
      await gantryApi.recoverCalibrationLimit(delta);
      setAlarmPrompt(null);
      if (resolvedStatus) {
        setResolvedAlarmStatus(resolvedStatus);
      }
      setStatusNote(
        "Backed off the limit switch. You can continue.",
      );
    } catch (recoveryErr) {
      const recoveryMessage = recoveryErr instanceof Error ? recoveryErr.message : String(recoveryErr);
      setResolvedAlarmStatus(null);
      // A lost serial link needs a power-cycle, not an E-stop — tell the
      // operator the actionable step instead of the generic lockout text.
      setAlarmPrompt(
        /connection lost/i.test(recoveryMessage)
          ? recoveryMessage
          : "Could not back off the limit switch. Press E-stop or reset the controller, then try again.",
      );
      setError(`Limit recovery failed after jog error (${message}): ${recoveryMessage}`);
    } finally {
      setOperation(null);
      setBusy(false);
      recoveryInProgress.current = false;
    }
  }, [stopJog]);

  useEffect(() => {
    if (!open || !connected || busy || !isAlarm || alarmPrompt || recoveryInProgress.current) return;
    stopJog();
    const delta = lastJogDelta.current;
    if (!delta || isZeroDelta(delta)) {
      setAlarmPrompt(
        "The gantry hit a limit switch and cannot back off on its own. Press E-stop or reset the controller, then continue.",
      );
      return;
    }
    const key = `${status}|${delta.x}|${delta.y}|${delta.z}`;
    if (recoveryAttemptKey.current === key) return;
    recoveryAttemptKey.current = key;
    void recoverFromLimitAlarm(delta, new Error(status), status);
  }, [alarmPrompt, busy, connected, isAlarm, open, recoverFromLimitAlarm, status, stopJog]);

  const reportError = useCallback((err: unknown) => {
    stopJog();
    const message = err instanceof Error ? err.message : String(err);
    if (looksLikeAlarm(message)) {
      setAlarmPrompt(
        "The gantry is in an alarm state. Clear it before continuing.",
      );
    }
    setError(message);
  }, [stopJog]);

  const clearPendingCaptures = () => {
    pendingSingleOrigin.current = null;
    pendingZReference.current = null;
  };

  const resetFlow = () => {
    setStep(0);
    setOperation(null);
    setError(null);
    setAlarmPrompt(null);
    setStatusNote(null);
    setXyOrigin(null);
    setZReference(null);
    setBlockTouch(null);
    setCalibrationHome(null);
    setZCalibration(null);
    setXyBounds(null);
    setInstrumentPositions({});
    setCameraBlockDistances({});
    setOutputFile(defaultOutputFilename(filename));
    setReferenceInstrument(contactInstruments[0] ?? "");
    setLowestInstrument(contactInstruments[0] ?? "");
    setResolvedAlarmStatus(null);
    clearPendingCaptures();
    lastJogDelta.current = null;
    jogRequestCount.current = 0;
    recoveryAttemptKey.current = null;
  };

  const reset = async () => {
    if (busy) return;
    stopJog();
    let restoreError: string | null = null;
    if (connected) {
      try {
        await gantryApi.restoreCalibrationSoftLimits();
      } catch (err) {
        restoreError = `Failed to restore soft limits: ${err instanceof Error ? err.message : String(err)}. Reconnect before running protocols.`;
      }
    }
    // Reset even when the restore failed (e.g. the serial link died after
    // a limit crash) — refusing traps the operator in a wizard whose every
    // exit needs a controller round-trip. The session still tracks the
    // un-restored state and the gantry widget's CALIBRATION INTERRUPTED
    // banner offers the retry.
    resetFlow();
    if (restoreError) {
      setError(restoreError);
    }
  };

  const close = async () => {
    stopJog();
    if (!connected) {
      onClose();
      return;
    }
    try {
      await gantryApi.restoreCalibrationSoftLimits();
    } catch {
      // Closing must never be gated on a controller round-trip. If the
      // restore failed, calibration_active stays true on the session and
      // the gantry widget's CALIBRATION INTERRUPTED banner takes over
      // until soft limits are actually restored.
    }
    onClose();
  };

  useEffect(() => {
    if (!open || step < 2) return;
    const warnBeforeUnload = (event: BeforeUnloadEvent) => {
      event.preventDefault();
      event.returnValue = "";
      return "";
    };
    window.addEventListener("beforeunload", warnBeforeUnload);
    return () => window.removeEventListener("beforeunload", warnBeforeUnload);
  }, [open, step]);

  useEffect(() => {
    if (!open) return;
    previousFocusRef.current = document.activeElement instanceof HTMLElement
      ? document.activeElement
      : null;
    const focusTimer = window.setTimeout(() => {
      dialogRef.current?.focus();
    }, 0);
    return () => {
      window.clearTimeout(focusTimer);
      previousFocusRef.current?.focus();
    };
  }, [open]);

  const handleDialogKeyDown = (event: ReactKeyboardEvent<HTMLDivElement>) => {
    if (event.key === "Escape") {
      event.preventDefault();
      if (!busy) {
        void close();
      }
      return;
    }
    if (event.key !== "Tab") return;
    const dialog = dialogRef.current;
    if (!dialog) return;
    const focusable = getFocusableElements(dialog);
    if (focusable.length === 0) {
      event.preventDefault();
      dialog.focus();
      return;
    }
    const first = focusable[0];
    const last = focusable[focusable.length - 1];
    const active = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    const activeIsFocusable = active ? focusable.includes(active) : false;
    if (event.shiftKey && (!activeIsFocusable || active === first)) {
      event.preventDefault();
      last.focus();
      return;
    }
    if (!event.shiftKey && (!activeIsFocusable || active === last)) {
      event.preventDefault();
      first.focus();
    }
  };

  const runAction = async (label: string, action: () => Promise<void>) => {
    lastJogDelta.current = null;
    setBusy(true);
    setOperation(label);
    setError(null);
    try {
      await action();
    } catch (err) {
      reportError(err);
    } finally {
      setOperation(null);
      setBusy(false);
    }
  };

  const unlockAlarm = () => runAction("Clearing alarm", async () => {
    await gantryApi.unlock();
    setAlarmPrompt(null);
    setStatusNote("Alarm cleared. Jog Z+ away from the limit before continuing.");
  });

  const jogBlockingWithRecovery = async (delta: JogDelta, timeout_s: number): Promise<boolean> => {
    rememberJogDelta(delta);
    try {
      await gantryApi.jogBlocking(delta.x, delta.y, delta.z, timeout_s);
      return false;
    } catch (err) {
      if (looksLikeAlarm(errorMessage(err))) {
        await recoverFromLimitAlarm(delta, err);
        return true;
      }
      throw err;
    }
  };

  // Step 0 → 1: just navigate, no hardware action yet.
  const goToHome = () => {
    if (!filename || instruments.length === 0) return;
    if (isMulti && contactInstruments.length === 0) {
      setError("Multi-instrument calibration requires at least one contact-capable instrument.");
      return;
    }
    if (isMulti && config) {
      try {
        // Missing homing_pull_off now falls back to a default; this only
        // catches an explicitly invalid (negative/non-finite) value.
        getConfiguredHomingPullOff(config);
      } catch (err) {
        setError(
          `grbl_settings.homing_pull_off in the gantry YAML is invalid. ` +
          `Fix the config and save before calibrating. (${errorMessage(err)})`,
        );
        return;
      }
    }
    setStatusNote(null);
    setStep(1);
  };

  // Step 1: connect (if needed) then home. This is the first hardware action.
  const homeForCalibration = () => runAction("Homing", async () => {
    if (!filename) throw new Error("Select a gantry config first.");
    if (!connected) {
      await gantryApi.connect(filename);
    }
    const result = await gantryApi.prepareCalibrationOrigin();
    const homed = requirePosition(result);
    setCalibrationHome(homed);
    setStatusNote("Homed.");
    setStep(2);
  });

  const setXY = () => runAction(
    "Setting XY origin",
    async () => {
    const result = await gantryApi.setWorkCoordinates({ x: 0, y: 0 });
    const captured = requirePosition(result);
    setXyOrigin(captured);
    const centered = await gantryApi.homeAndCenterForCalibration();
    setXyBounds(capturedFromPlain(centered.xy_bounds));
    setStatusNote("XY origin set.");
    setStep(3);
  });

  // Shared "Block height" step handler for both flows: validate the
  // entered height, then advance (single → Set origin, multi → Z ref).
  const continueFromBlockHeight = (nextStep: number) => {
    setError(null);
    try {
      parseBlockHeight(blockHeight);
    } catch (err) {
      setError(errorMessage(err));
      return;
    }
    setStatusNote(null);
    setStep(nextStep);
  };

  const setSingleInstrumentOrigin = () => runAction("Setting origin", async () => {
    if (!config) throw new Error("No gantry config is loaded.");
    let pending = pendingSingleOrigin.current;
    if (!pending) {
      const height = parseBlockHeight(blockHeight);
      const touchTipLength = soleTipAttached ? parseTipLength(tipLengths[soleInstrument] ?? "") : 0;
      const rawTouch = requirePosition(await gantryApi.getPosition());
      const blockTouch = { ...rawTouch, z: rawTouch.z - touchTipLength };
      // Set WPos before restoring soft limits: if setWorkCoordinates fails,
      // soft limits stay disabled so the operator can still jog freely and
      // retry. Once this succeeds, retries must not re-capture in the shifted
      // frame.
      const result = await gantryApi.setWorkCoordinates({ x: 0, y: 0, z: height });
      pending = {
        blockTouch,
        zReference: requirePosition(result),
      };
      pendingSingleOrigin.current = pending;
    }
    await gantryApi.restoreCalibrationSoftLimits();
    setBlockTouch(pending.blockTouch);
    setZReference(pending.zReference);
    setZCalibration(null);
    setStatusNote("Origin set.");
    setStep(4);
  });

  const setZ = () => runAction(
    isMulti && selectedLowest
      ? `Setting Z reference with ${selectedLowest}`
      : "Setting Z reference",
    async () => {
    if (!config) throw new Error("No gantry config is loaded.");
    let pending = pendingZReference.current;
    if (!pending) {
      const height = parseBlockHeight(blockHeight);
      const factoryZTravel = getFactoryZTravel(config);
      const touchTipLength = isMulti && lowestTipAttached ? parseTipLength(tipLengths[selectedLowest] ?? "") : 0;
      const rawTouch = requirePosition(await gantryApi.getPosition());
      const blockTouch = { ...rawTouch, z: rawTouch.z - touchTipLength };
      const homeZ = isMulti
        ? requireCaptured(xyBounds, "Homed XY bounds")
        : requireCaptured(calibrationHome, "Home position");
      const calibration = calculateSingleInstrumentZCalibration({
        homeZ,
        blockTouchZ: blockTouch.z,
        blockHeight: height,
        factoryZTravel,
      });
      const result = await gantryApi.setWorkCoordinates({ z: height });
      pending = {
        blockTouch,
        zReference: requirePosition(result),
        calibration,
        lowestInstrument: selectedLowest,
      };
      pendingZReference.current = pending;
    }
    setZReference(pending.zReference);
    setZCalibration(pending.calibration);
    if (isMulti && pending.lowestInstrument) {
      setInstrumentPositions((prev) => ({ ...prev, [pending.lowestInstrument]: pending.zReference }));
      const recovered = await jogBlockingWithRecovery({ x: 0, y: 0, z: 15 }, 15);
      setStatusNote(
        recovered
          ? `Recorded ${pending.lowestInstrument}. The gantry hit a limit while lifting and backed off.`
          : `Recorded ${pending.lowestInstrument}.`,
      );
    } else {
      setStatusNote("Z reference set.");
    }
    setStep(isMulti ? 5 : 3);
  });

  const recordCurrentInstrument = (name: string) => runAction(
    `Recording ${name}`,
    async () => {
    if (!name) return;
    const isCamera = nonContactInstruments.includes(name);
    if (isCamera) {
      const message = validateCameraBlockDistance(cameraBlockDistances[name]);
      if (message) throw new Error(message);
    }
    if (tipAttached[name]) {
      const message = validateTipLength(tipLengths[name]);
      if (message) throw new Error(message);
    }
    const captured = requirePosition(await gantryApi.getPosition());
    const nextPositions = { ...instrumentPositions, [name]: captured };
    setInstrumentPositions(nextPositions);
    if (isCamera) {
      setStatusNote(`Recorded ${name}.`);
      if (allInstrumentPositionsReady(calibratableInstruments, nextPositions, selectedReference, selectedLowest)) {
        setStep(6);
      }
      return;
    }
    const recovered = await jogBlockingWithRecovery({ x: 0, y: 0, z: 15 }, 15);
    setStatusNote(
      recovered
        ? `Recorded ${name}. The gantry hit a limit while lifting and backed off.`
        : `Recorded ${name}.`,
    );
    if (allInstrumentPositionsReady(calibratableInstruments, nextPositions, selectedReference, selectedLowest)) {
      setStep(6);
    }
  });

  const save = () => runAction("Measuring travel and saving", async () => {
    if (!config) throw new Error("No gantry config is loaded.");
    if (!readyForSave) throw new Error("Complete the calibration positions before saving.");
    if (!isMulti) {
      const homeZ = requireCaptured(calibrationHome, "Home position");
      const blockTouchZ = requireCaptured(blockTouch, "Block touch position");
      const height = parseBlockHeight(blockHeight);
      const factoryZTravel = getFactoryZTravel(config);
      const finalized = await gantryApi.finalizeCalibrationOrigin({
        home_z: homeZ,
        block_touch_z: blockTouchZ,
        block_height: height,
        factory_z_travel: factoryZTravel,
      });
      const measuredVolume = capturedFromPlain(finalized.measured_volume);
      const maxTravel = capturedFromPlain(finalized.max_travel);
      await onSaveCalibrated(normalizedOutput, buildCalibratedConfig({
        config: {
          ...config,
          cnc: {
            ...config.cnc,
            calibration_block_height_mm: finalized.z_calibration.block_height,
          },
          grbl_settings: {
            ...(config.grbl_settings ?? {}),
            homing_pull_off: finalized.homing_pull_off_mm ?? config.grbl_settings?.homing_pull_off,
          },
        },
        measuredVolume,
        zMin: finalized.z_calibration.z_min,
        zMax: finalized.z_calibration.z_max,
        maxTravel,
        isMulti: false,
        instruments,
        instrumentPositions,
        referenceInstrument: selectedReference,
        lowestInstrument: selectedLowest,
        cameraBlockDistances: parsedCameraBlockDistances(cameraBlockDistances, nonContactInstruments),
      }));
      onClose();
      return;
    }
    const result = await gantryApi.home();
    const captured = requirePosition(result);
    const initialZCalibration = requireZCalibration(zCalibration);
    const calibratedConfig = {
      ...config,
      cnc: {
        ...config.cnc,
        calibration_block_height_mm: initialZCalibration.blockHeight,
      },
    };
    const finalZCalibration = calculateSingleInstrumentZCalibration({
      homeZ: initialZCalibration.homeZ,
      blockTouchZ: initialZCalibration.blockTouchZ,
      blockHeight: initialZCalibration.blockHeight,
      factoryZTravel: initialZCalibration.factoryZTravel,
      homedZ: captured.z,
    });
    const zMin = finalZCalibration.zMin;
    const zMax = finalZCalibration.zMax;
    const homingPullOff = getConfiguredHomingPullOff(config);
    const maxTravel = {
      x: roundMm(captured.x + homingPullOff),
      y: roundMm(captured.y + homingPullOff),
      z: roundMm(finalZCalibration.maxTravelZ + homingPullOff),
    };
    if (maxTravel.x <= 0 || maxTravel.y <= 0 || maxTravel.z <= 0) {
      throw new Error("Measured travel spans must be positive.");
    }
    await gantryApi.configureSoftLimits({
      max_travel_x: maxTravel.x,
      max_travel_y: maxTravel.y,
      max_travel_z: maxTravel.z,
      status_report: 0,
      homing_pull_off: homingPullOff,
    });
    await onSaveCalibrated(normalizedOutput, buildCalibratedConfig({
      config: {
        ...calibratedConfig,
        grbl_settings: {
          ...(calibratedConfig.grbl_settings ?? {}),
          homing_pull_off: homingPullOff,
        },
      },
      measuredVolume: captured,
      zMin,
      zMax,
      maxTravel,
      isMulti,
      instruments,
      instrumentPositions,
      referenceInstrument: selectedReference,
      lowestInstrument: selectedLowest,
      cameraBlockDistances: parsedCameraBlockDistances(cameraBlockDistances, nonContactInstruments),
      tipLengths: parsedTipLengths(tipLengths, tipAttached),
    }));
    onClose();
  });

  const jog = useCallback(async (x: number, y: number, z: number): Promise<boolean> => {
    if (!connected || busy || alarmRecoveryMessage || recoveryInProgress.current) return false;
    rememberJogDelta({ x, y, z });
    jogRequestCount.current += 1;
    try {
      await gantryApi.jog(x, y, z);
      return true;
    } catch (err) {
      if (looksLikeAlarm(errorMessage(err))) {
        void recoverFromLimitAlarm({ x, y, z }, err);
      } else {
        reportError(err);
      }
      return false;
    }
  }, [alarmRecoveryMessage, busy, connected, recoverFromLimitAlarm, rememberJogDelta, reportError]);

  // The held-jog pump reads jog through a ref so an in-flight hold always
  // uses the latest guards (alarm/busy state) instead of a stale closure.
  const jogRef = useRef(jog);
  useEffect(() => {
    jogRef.current = jog;
  }, [jog]);

  const startJog = (x: number, y: number, z: number) => {
    if (busy || alarmRecoveryMessage || recoveryInProgress.current) return;
    const heldBefore = jogHeld.current !== null;
    jogHeld.current = { x, y, z };
    if (heldBefore && jogPumpActive.current) {
      // Direction change mid-hold: hand the new delta to the pump instead
      // of firing immediately — an immediate send would repeat at the
      // previous segment's pace, and with soft limits off during
      // calibration that backlog runs the gantry into the hard-limit
      // switches. wake() cuts the remaining sleep so the new direction
      // still starts promptly.
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
          // Held repeats stay one-in-flight, paced by the segment actually
          // sent last (the held delta can change mid-hold) — soft limits
          // are off during calibration, so an unbounded jog backlog here
          // runs the gantry into the hard-limit switches.
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
  };

  const parsedXyStep = parsePositiveStep(xyStep);
  const parsedZStep = parsePositiveStep(zStep);
  const xy = parsedXyStep == null ? MIN_STEP : Math.max(MIN_STEP, parsedXyStep);
  const z = parsedZStep == null ? MIN_STEP : Math.max(MIN_STEP, parsedZStep);
  const stepInvalid = parsedXyStep == null || parsedZStep == null;
  const xyBelowMin = parsedXyStep != null && parsedXyStep < MIN_STEP;
  const zBelowMin = parsedZStep != null && parsedZStep < MIN_STEP;

  if (!open) return null;

  return (
    <div
      ref={dialogRef}
      style={overlayStyle}
      role="dialog"
      aria-modal="true"
      aria-label="Gantry calibration"
      tabIndex={-1}
      onKeyDown={handleDialogKeyDown}
    >
      <div style={modalStyle}>
        <div style={headerStyle}>
          <div>
            <h2 style={{ margin: 0, fontSize: 18, color: theme.color.ink, letterSpacing: "-0.01em" }}>Calibrate gantry</h2>
            <div style={{ marginTop: 3, fontSize: 12, color: theme.color.textMuted }}>
              {filename || "No file selected"}
            </div>
          </div>
          <div style={headerActionsStyle}>
            <button
              onClick={() => void reset()}
              disabled={busy}
              style={buttonStateStyle(buttonStyle, busy)}
            >
              Reset wizard
            </button>
            <button
              onClick={close}
              style={closeButtonStyle}
              aria-label="Close calibration"
            >
              ×
            </button>
          </div>
        </div>

        <div style={bodyStyle}>
          <aside style={stepsStyle}>
            {stepLabels(isMulti).map((label, index) => (
              <div
                key={label}
                style={index === step ? activeStepStyle : index < step ? completedStepStyle : stepButtonStyle}
                aria-current={index === step ? "step" : undefined}
              >
                <span style={stepNumberStyle}>{index + 1}</span>
                {label}
              </div>
            ))}
          </aside>

          <section style={contentStyle}>
            {alarmRecoveryMessage && (
              <div style={alarmStyle}>
                <span style={alarmTitleStyle}>GANTRY ALARM</span>
                <span>{alarmRecoveryMessage}</span>
                <button
                  onClick={unlockAlarm}
                  disabled={busy}
                  style={buttonStateStyle(alarmButtonStyle, busy)}
                >
                  Unlock alarm
                </button>
              </div>
            )}
            {!alarmRecoveryMessage && connected && (
              <div style={softLimitNoticeStyle}>
                Jog in small steps near the edges of travel. The gantry stops at its
                limit switches and backs off on its own.
              </div>
            )}
            {error && <div style={errorStyle}>{error}</div>}
            {operation && <div style={busyStyle}>{operation}… Please wait.</div>}
            {statusNote && <div style={noteStyle}>{statusNote}</div>}

            {step === 0 && (
              <div>
                <h3 style={sectionTitleStyle}>Prepare</h3>
                <p style={instructionStyle}>
                  {isMulti
                    ? "Choose a file name for the calibrated config, pick the reference and lowest instruments, then continue."
                    : "Choose a file name for the calibrated config, then continue."}
                </p>
                {instruments.length === 0 && (
                  <div style={noInstrumentsStyle}>
                    Add and save at least one mounted instrument in the Gantry configuration before calibrating.
                  </div>
                )}
                <div style={fieldRowStyle}>
                  <label style={fieldStyle}>
                    <span style={labelStyle}>Output YAML</span>
                    <input
                      value={outputFile}
                      onChange={(event) => setOutputFile(event.target.value)}
                      disabled={controlsLocked}
                      style={buttonStateStyle(inputStyle, controlsLocked)}
                    />
                  </label>
                  {isMulti && (
                    <>
                      <label style={fieldStyle}>
                        <span style={labelStyle}>Reference instrument</span>
                        <select
                          value={selectedReference}
                          onChange={(event) => setReferenceInstrument(event.target.value)}
                          disabled={controlsLocked}
                          style={buttonStateStyle(inputStyle, controlsLocked)}
                        >
                          {contactInstruments.map((name) => <option key={name} value={name}>{name}</option>)}
                        </select>
                      </label>
                      <label style={fieldStyle}>
                        <span style={labelStyle}>Lowest instrument</span>
                        <select
                          value={selectedLowest}
                          onChange={(event) => setLowestInstrument(event.target.value)}
                          disabled={controlsLocked}
                          style={buttonStateStyle(inputStyle, controlsLocked)}
                        >
                          {contactInstruments.map((name) => <option key={name} value={name}>{name}</option>)}
                        </select>
                      </label>
                    </>
                  )}
                </div>
                <div style={actionRowStyle}>
                  <button onClick={goToHome} disabled={controlsLocked || !filename || instruments.length === 0} style={buttonStateStyle(primaryButtonStyle, controlsLocked || !filename || instruments.length === 0)}>Continue</button>
                </div>
              </div>
            )}

            {step === 1 && (
              <div>
                <h3 style={sectionTitleStyle}>Home gantry</h3>
                <p style={instructionStyle}>
                  Clear the deck so nothing is in the gantry's path, then home.
                </p>
                <div style={actionRowStyle}>
                  <button onClick={homeForCalibration} disabled={controlsLocked} style={buttonStateStyle(primaryButtonStyle, controlsLocked)}>Home gantry</button>
                </div>
              </div>
            )}

            {!isMulti && step === 2 && (
              <div>
                <h3 style={sectionTitleStyle}>Calibration Reference Height</h3>
                <p style={instructionStyle}>
                  Enter the height of your calibration block above the deck. If you are touching another surface instead (for example the top of a plate), enter its height from the deck.
                </p>
                <div style={fieldRowStyle}>
                  <label style={fieldStyle}>
                    <span style={labelStyle}>Reference height (mm)</span>
                    <input
                      value={blockHeight}
                      onChange={(event) => setBlockHeight(event.target.value)}
                      style={inputStyle}
                      inputMode="decimal"
                      autoFocus
                    />
                  </label>
                </div>
                <div style={actionRowStyle}>
                  <button onClick={() => continueFromBlockHeight(3)} disabled={controlsLocked} style={buttonStateStyle(primaryButtonStyle, controlsLocked)}>Continue</button>
                </div>
              </div>
            )}

            {!isMulti && step === 3 && (
              <div>
                <h3 style={sectionTitleStyle}>Set Origin</h3>
                <p style={instructionStyle}>
                  {originPolicy === "home_origin"
                    ? "Put the calibration block at the back-right corner of the deck. Jog the tool until it just touches the top of the block, then continue."
                    : "Put the calibration block at the front-left corner of the deck. Jog the tool until it just touches the top of the block, then continue."}
                </p>
                {soleInstrumentIsPipette && (
                  <p style={{ ...instructionStyle, margin: "8px 0 0" }}>
                    If this pipette can't reach the block bare, attach a tip and touch the block with the tip
                    instead. Enter the tip length below and it's subtracted so the calibrated Z frame still
                    reflects the bare-nozzle position.
                  </p>
                )}
                {soleInstrumentIsPipette && (
                  <div style={{ margin: "12px 0" }}>
                    <label style={{ display: "flex", alignItems: "center", gap: 8, cursor: "pointer" }}>
                      <input
                        type="checkbox"
                        aria-label="Calibrating with a tip attached"
                        checked={soleTipAttached}
                        onChange={(event) => setTipAttached((prev) => ({
                          ...prev,
                          [soleInstrument]: event.target.checked,
                        }))}
                        disabled={controlsLocked}
                      />
                      <span style={labelStyle}>Calibrating with a tip attached</span>
                    </label>
                    {soleTipAttached && (
                      <label style={{ ...fieldStyle, marginTop: 8 }}>
                        <span style={labelStyle}>Tip length (mm)</span>
                        <input
                          aria-label="Tip length (mm)"
                          value={tipLengths[soleInstrument] ?? ""}
                          onChange={(event) => setTipLengths((prev) => ({
                            ...prev,
                            [soleInstrument]: event.target.value,
                          }))}
                          disabled={controlsLocked}
                          inputMode="decimal"
                          style={buttonStateStyle(inputStyle, controlsLocked)}
                        />
                        {soleTipLengthError && (
                          <span style={{ color: theme.color.danger, fontSize: 12 }}>{soleTipLengthError}</span>
                        )}
                      </label>
                    )}
                  </div>
                )}
                <JogPanel
                  xyStep={xyStep}
                  zStep={zStep}
                  setXyStep={setXyStep}
                  setZStep={setZStep}
                  disabled={!connected || controlsLocked}
                  alarmed={!!alarmRecoveryMessage}
                  onStartJog={startJog}
                  onStopJog={stopJog}
                  xy={xy}
                  z={z}
                  stepInvalid={stepInvalid}
                  xyStepInvalid={parsedXyStep == null}
                  zStepInvalid={parsedZStep == null}
                  xyBelowMin={xyBelowMin}
                  zBelowMin={zBelowMin}
                />
                <div style={actionRowStyle}>
                  <button
                    onClick={setSingleInstrumentOrigin}
                    disabled={controlsLocked || !connected || !!soleTipLengthError}
                    style={buttonStateStyle(primaryButtonStyle, controlsLocked || !connected || !!soleTipLengthError)}
                  >
                    Set origin and continue
                  </button>
                </div>
              </div>
            )}

            {isMulti && step === 2 && (
              <div>
                <h3 style={sectionTitleStyle}>Set XY Origin</h3>
                <p style={instructionStyle}>
                  {originPolicy === "home_origin"
                    ? "Put the calibration block at the back-right corner of the deck. Jog the tool until it is directly over the mark on the block, then continue."
                    : "Put the calibration block at the front-left corner of the deck. Jog the tool until it is directly over the mark on the block, then continue."}
                </p>
                <JogPanel
                  xyStep={xyStep}
                  zStep={zStep}
                  setXyStep={setXyStep}
                  setZStep={setZStep}
                  disabled={!connected || controlsLocked}
                  alarmed={!!alarmRecoveryMessage}
                  onStartJog={startJog}
                  onStopJog={stopJog}
                  xy={xy}
                  z={z}
                  stepInvalid={stepInvalid}
                  xyStepInvalid={parsedXyStep == null}
                  zStepInvalid={parsedZStep == null}
                  xyBelowMin={xyBelowMin}
                  zBelowMin={zBelowMin}
                />
                <div style={actionRowStyle}>
                  <button onClick={setXY} disabled={controlsLocked || !connected} style={buttonStateStyle(primaryButtonStyle, controlsLocked || !connected)}>Set XY origin and continue</button>
                </div>
              </div>
            )}

            {isMulti && step === 3 && (
              <div>
                <h3 style={sectionTitleStyle}>Calibration Block Height</h3>
                <p style={instructionStyle}>
                  Enter the height of the calibration block above the deck.
                </p>
                <div style={fieldRowStyle}>
                  <label style={fieldStyle}>
                    <span style={labelStyle}>Block height (mm)</span>
                    <input
                      value={blockHeight}
                      onChange={(event) => setBlockHeight(event.target.value)}
                      style={inputStyle}
                      inputMode="decimal"
                      autoFocus
                    />
                  </label>
                </div>
                <div style={actionRowStyle}>
                  <button onClick={() => continueFromBlockHeight(4)} disabled={controlsLocked} style={buttonStateStyle(primaryButtonStyle, controlsLocked)}>Continue</button>
                </div>
              </div>
            )}

            {isMulti && step === 4 && (
              <div>
                <h3 style={sectionTitleStyle}>Set Z Reference</h3>
                <p style={instructionStyle}>
                  Keep the block where it is. Jog {selectedLowest || "the lowest instrument"} until it just touches the top of the block, then continue.
                </p>
                {lowestInstrumentIsPipette && (
                  <p style={{ ...instructionStyle, margin: "8px 0 0" }}>
                    If this pipette can't reach the block bare, attach a tip and touch the block with the tip
                    instead. Enter the tip length below and it's subtracted so the calibrated Z frame still
                    reflects the bare-nozzle position.
                  </p>
                )}
                {lowestInstrumentIsPipette && (
                  <div style={{ margin: "12px 0" }}>
                    <label style={{ display: "flex", alignItems: "center", gap: 8, cursor: "pointer" }}>
                      <input
                        type="checkbox"
                        aria-label="Calibrating with a tip attached"
                        checked={lowestTipAttached}
                        onChange={(event) => setTipAttached((prev) => ({
                          ...prev,
                          [selectedLowest]: event.target.checked,
                        }))}
                        disabled={controlsLocked}
                      />
                      <span style={labelStyle}>Calibrating with a tip attached</span>
                    </label>
                    {lowestTipAttached && (
                      <label style={{ ...fieldStyle, marginTop: 8 }}>
                        <span style={labelStyle}>Tip length (mm)</span>
                        <input
                          aria-label="Tip length (mm)"
                          value={tipLengths[selectedLowest] ?? ""}
                          onChange={(event) => setTipLengths((prev) => ({
                            ...prev,
                            [selectedLowest]: event.target.value,
                          }))}
                          disabled={controlsLocked}
                          inputMode="decimal"
                          style={buttonStateStyle(inputStyle, controlsLocked)}
                        />
                        {lowestTipLengthError && (
                          <span style={{ color: theme.color.danger, fontSize: 12 }}>{lowestTipLengthError}</span>
                        )}
                      </label>
                    )}
                  </div>
                )}
                <JogPanel
                  xyStep={xyStep}
                  zStep={zStep}
                  setXyStep={setXyStep}
                  setZStep={setZStep}
                  disabled={!connected || controlsLocked}
                  alarmed={!!alarmRecoveryMessage}
                  onStartJog={startJog}
                  onStopJog={stopJog}
                  xy={xy}
                  z={z}
                  stepInvalid={stepInvalid}
                  xyStepInvalid={parsedXyStep == null}
                  zStepInvalid={parsedZStep == null}
                  xyBelowMin={xyBelowMin}
                  zBelowMin={zBelowMin}
                />
                <div style={actionRowStyle}>
                  <button
                    onClick={setZ}
                    disabled={controlsLocked || !connected || !xyOrigin || !!lowestTipLengthError}
                    style={buttonStateStyle(primaryButtonStyle, controlsLocked || !connected || !xyOrigin || !!lowestTipLengthError)}
                  >
                    {`Set Z reference with ${selectedLowest} and continue`}
                  </button>
                </div>
              </div>
            )}

            {isMulti && step === 5 && (
              <div>
                <h3 style={sectionTitleStyle}>Record Instruments</h3>
                <p style={instructionStyle}>
                  Keep the block where it is. Jog each instrument until it just touches the top of the block, then record it.
                </p>
                <div style={instrumentListStyle}>
                  {calibratableInstruments.map((name) => (
                    <div key={name} style={instrumentRowStyle}>
                      <strong>{name}</strong>
                      <span style={{ ...theme.mono, color: theme.color.textMuted }}>
                        {instrumentPositions[name]
                          ? "recorded"
                          : name === nextInstrumentToRecord
                            ? "up next"
                            : "pending"}
                      </span>
                    </div>
                  ))}
                </div>
                {followCameraInstruments.length > 0 && (
                  <p style={instructionStyle}>
                    {followCameraInstruments.join(", ")}: calibrated automatically with the camera.
                  </p>
                )}
                {nextInstrumentToRecord ? (
                  <div style={activeInstrumentStyle}>
                    <div style={{ marginBottom: 10 }}>
                      <span style={labelStyle}>Active instrument</span>
                      <h4 style={{ margin: "2px 0 0", fontSize: 15, color: theme.color.ink }}>{nextInstrumentToRecord}</h4>
                      {nextInstrumentIsCamera && (
                        <p style={{ ...instructionStyle, margin: "8px 0 0" }}>
                          Center the camera over the calibration block mark, then enter the distance from the camera to the top of the block.
                        </p>
                      )}
                      {nextInstrumentIsPipette && (
                        <p style={{ ...instructionStyle, margin: "8px 0 0" }}>
                          If this pipette can't reach the block bare, attach a tip and touch the block with the tip
                          instead. Enter the tip length below and it's subtracted so the saved depth still reflects
                          the bare-nozzle position.
                        </p>
                      )}
                    </div>
                    {nextInstrumentIsCamera && (
                      <CameraPreview key={nextInstrumentToRecord} instrument={nextInstrumentToRecord} />
                    )}
                    {nextInstrumentIsCamera && (
                      <label style={{ ...fieldStyle, marginBottom: 12 }}>
                        <span style={labelStyle}>Distance from calibration block (mm)</span>
                        <input
                          aria-label="Distance from calibration block (mm)"
                          value={cameraBlockDistances[nextInstrumentToRecord] ?? ""}
                          onChange={(event) => setCameraBlockDistances((prev) => ({
                            ...prev,
                            [nextInstrumentToRecord]: event.target.value,
                          }))}
                          disabled={controlsLocked}
                          inputMode="decimal"
                          style={buttonStateStyle(inputStyle, controlsLocked)}
                        />
                        {nextCameraDistanceError && (
                          <span style={{ color: theme.color.danger, fontSize: 12 }}>{nextCameraDistanceError}</span>
                        )}
                      </label>
                    )}
                    {nextInstrumentIsPipette && (
                      <div style={{ marginBottom: 12 }}>
                        <label style={{ display: "flex", alignItems: "center", gap: 8, cursor: "pointer" }}>
                          <input
                            type="checkbox"
                            aria-label="Calibrating with a tip attached"
                            checked={nextInstrumentTipAttached}
                            onChange={(event) => setTipAttached((prev) => ({
                              ...prev,
                              [nextInstrumentToRecord]: event.target.checked,
                            }))}
                            disabled={controlsLocked}
                          />
                          <span style={labelStyle}>Calibrating with a tip attached</span>
                        </label>
                        {nextInstrumentTipAttached && (
                          <label style={{ ...fieldStyle, marginTop: 8 }}>
                            <span style={labelStyle}>Tip length (mm)</span>
                            <input
                              aria-label="Tip length (mm)"
                              value={tipLengths[nextInstrumentToRecord] ?? ""}
                              onChange={(event) => setTipLengths((prev) => ({
                                ...prev,
                                [nextInstrumentToRecord]: event.target.value,
                              }))}
                              disabled={controlsLocked}
                              inputMode="decimal"
                              style={buttonStateStyle(inputStyle, controlsLocked)}
                            />
                            {nextTipLengthError && (
                              <span style={{ color: theme.color.danger, fontSize: 12 }}>{nextTipLengthError}</span>
                            )}
                          </label>
                        )}
                      </div>
                    )}
                    <JogPanel
                      xyStep={xyStep}
                      zStep={zStep}
                      setXyStep={setXyStep}
                      setZStep={setZStep}
                      disabled={!connected || controlsLocked}
                      alarmed={!!alarmRecoveryMessage}
                      onStartJog={startJog}
                      onStopJog={stopJog}
                      xy={xy}
                      z={z}
                      stepInvalid={stepInvalid}
                      xyStepInvalid={parsedXyStep == null}
                      zStepInvalid={parsedZStep == null}
                      xyBelowMin={xyBelowMin}
                      zBelowMin={zBelowMin}
                    />
                    <div style={actionRowStyle}>
                      <button
                        onClick={() => recordCurrentInstrument(nextInstrumentToRecord)}
                        disabled={controlsLocked || !connected || !!nextCameraDistanceError || !!nextTipLengthError}
                        style={buttonStateStyle(primaryButtonStyle, controlsLocked || !connected || !!nextCameraDistanceError || !!nextTipLengthError)}
                      >
                        {`Record ${nextInstrumentToRecord}`}
                      </button>
                    </div>
                  </div>
                ) : (
                  <div style={noteStyle}>All instruments recorded. Continue to save.</div>
                )}
              </div>
            )}

            {((!isMulti && step === 4) || (isMulti && step === 6)) && (
              <div>
                <h3 style={sectionTitleStyle}>Save</h3>
                <p style={instructionStyle}>
                  Clear the deck. The gantry will home and measure its travel, then the calibration is saved to {normalizedOutput} and this window closes.
                </p>
                <div style={actionRowStyle}>
                  <button onClick={save} disabled={controlsLocked || !readyForSave} style={buttonStateStyle(primaryButtonStyle, controlsLocked || !readyForSave)}>
                    Save
                  </button>
                </div>
              </div>
            )}
          </section>
        </div>
      </div>
    </div>
  );
}

function JogPanel({
  xyStep,
  zStep,
  setXyStep,
  setZStep,
  disabled,
  alarmed,
  onStartJog,
  onStopJog,
  xy,
  z,
  stepInvalid,
  xyStepInvalid,
  zStepInvalid,
  xyBelowMin,
  zBelowMin,
}: {
  xyStep: string;
  zStep: string;
  setXyStep: (value: string) => void;
  setZStep: (value: string) => void;
  disabled: boolean;
  alarmed: boolean;
  onStartJog: (x: number, y: number, z: number) => void;
  onStopJog: () => void;
  xy: number;
  z: number;
  stepInvalid: boolean;
  xyStepInvalid: boolean;
  zStepInvalid: boolean;
  xyBelowMin: boolean;
  zBelowMin: boolean;
}) {
  const jogLocked = disabled || alarmed || stepInvalid;
  const props = (x: number, y: number, dz: number) => ({
    onMouseDown: () => !jogLocked && onStartJog(x, y, dz),
    onMouseUp: onStopJog,
    onMouseLeave: onStopJog,
    onTouchStart: (event: TouchEvent) => {
      event.preventDefault();
      if (!jogLocked) onStartJog(x, y, dz);
    },
    onTouchEnd: onStopJog,
  });

  return (
    <div style={jogPanelStyle}>
      <div style={dpadStyle}>
        <div />
        <button style={buttonStateStyle(jogButtonStyle, jogLocked)} disabled={jogLocked} {...props(0, xy, 0)} title="Y+">↑</button>
        <div />
        <button style={buttonStateStyle(jogButtonStyle, jogLocked)} disabled={jogLocked} {...props(-xy, 0, 0)} title="X-">←</button>
        <div style={padCenterStyle}>XY</div>
        <button style={buttonStateStyle(jogButtonStyle, jogLocked)} disabled={jogLocked} {...props(xy, 0, 0)} title="X+">→</button>
        <div />
        <button style={buttonStateStyle(jogButtonStyle, jogLocked)} disabled={jogLocked} {...props(0, -xy, 0)} title="Y-">↓</button>
        <div />
      </div>
      <div style={zPadStyle}>
        <button style={buttonStateStyle(jogButtonStyle, jogLocked)} disabled={jogLocked} {...props(0, 0, z)} title="Z+">Z+</button>
        <div style={padCenterStyle}>Z</div>
        <button style={buttonStateStyle(jogButtonStyle, jogLocked)} disabled={jogLocked} {...props(0, 0, -z)} title="Z-">Z-</button>
      </div>
      <div style={stepFieldsStyle}>
        <label style={stepFieldStyle}>
          <span style={labelStyle}>XY mm</span>
          <input
            value={xyStep}
            onChange={(event) => setXyStep(event.target.value)}
            disabled={disabled || alarmed}
            inputMode="decimal"
            style={buttonStateStyle({ ...smallInputStyle, borderColor: xyStepInvalid || xyBelowMin ? theme.color.danger : undefined }, disabled || alarmed)}
          />
        </label>
        <label style={stepFieldStyle}>
          <span style={labelStyle}>Z mm</span>
          <input
            value={zStep}
            onChange={(event) => setZStep(event.target.value)}
            disabled={disabled || alarmed}
            inputMode="decimal"
            style={buttonStateStyle({ ...smallInputStyle, borderColor: zStepInvalid || zBelowMin ? theme.color.danger : undefined }, disabled || alarmed)}
          />
        </label>
        {(stepInvalid || xyBelowMin || zBelowMin) && (
          <div style={stepHintStyle}>
            {stepInvalid ? "Enter step sizes greater than 0." : `Minimum jog step is ${MIN_STEP} mm.`}
          </div>
        )}
      </div>
    </div>
  );
}

function defaultOutputFilename(filename: string): string {
  return filename || "gantry.yaml";
}

function requirePosition(position: GantryPosition): CapturedPosition {
  if (!position.connected) throw new Error("Gantry is not connected.");
  if (position.work_x == null || position.work_y == null || position.work_z == null) {
    throw new Error("Work coordinate position is not available. Ensure homing and work coordinate setup completed before recording positions.");
  }
  return {
    x: Number(position.work_x),
    y: Number(position.work_y),
    z: Number(position.work_z),
  };
}

function requireCaptured(value: CapturedPosition | null, label: string): number {
  if (!value || !Number.isFinite(value.z)) {
    throw new Error(`${label} is not available. Home the gantry before recording the block touch.`);
  }
  return value.z;
}

function requireZCalibration(value: ZCalibrationResult | null): ZCalibrationResult {
  if (!value) {
    throw new Error("Z calibration is not available. Set the Z reference before saving.");
  }
  return value;
}

function capturedFromPlain(position: { x: number; y: number; z: number }): CapturedPosition {
  return {
    x: Number(position.x),
    y: Number(position.y),
    z: Number(position.z),
  };
}

function formatOptionalNumber(value: number | null | undefined): string {
  return value == null ? "" : String(value);
}

function parsePositiveStep(value: string): number | null {
  const parsed = Number(value);
  return Number.isFinite(parsed) && parsed > 0 ? parsed : null;
}

function parseBlockHeight(value: string): number {
  if (!value.trim()) {
    throw new Error("Enter a calibration reference height before continuing.");
  }
  const height = Number(value);
  if (!Number.isFinite(height) || height <= 0) {
    throw new Error("Calibration reference height must be greater than 0.");
  }
  return roundMm(height);
}

function parseCameraBlockDistance(value: string): number {
  if (!value.trim()) {
    throw new Error("Enter the distance from calibration block before recording camera position.");
  }
  const distance = Number(value);
  if (!Number.isFinite(distance) || distance < 0) {
    throw new Error("Distance from calibration block must be 0 or greater.");
  }
  return roundMm(distance);
}

function validateCameraBlockDistance(value: string | undefined): string | null {
  try {
    parseCameraBlockDistance(value ?? "");
    return null;
  } catch (err) {
    return errorMessage(err);
  }
}

function parsedCameraBlockDistances(
  values: Record<string, string>,
  cameras: string[],
): Record<string, number> {
  const parsed: Record<string, number> = {};
  for (const name of cameras) {
    if (values[name] == null || !values[name].trim()) continue;
    parsed[name] = parseCameraBlockDistance(values[name]);
  }
  return parsed;
}

function parseTipLength(value: string): number {
  if (!value.trim()) {
    throw new Error("Enter the tip length before recording a position calibrated with a tip attached.");
  }
  const length = Number(value);
  if (!Number.isFinite(length) || length <= 0) {
    throw new Error("Tip length must be greater than 0.");
  }
  return roundMm(length);
}

function validateTipLength(value: string | undefined): string | null {
  try {
    parseTipLength(value ?? "");
    return null;
  } catch (err) {
    return errorMessage(err);
  }
}

function parsedTipLengths(
  values: Record<string, string>,
  attached: Record<string, boolean>,
): Record<string, number> {
  const parsed: Record<string, number> = {};
  for (const name of Object.keys(attached)) {
    if (!attached[name]) continue;
    if (values[name] == null || !values[name].trim()) continue;
    parsed[name] = parseTipLength(values[name]);
  }
  return parsed;
}

function roundMm(value: number): number {
  return Math.round(value * 1000) / 1000;
}

function looksLikeAlarm(message: string): boolean {
  const lower = message.toLowerCase();
  return lower.includes("alarm") || lower.includes("hard limit") || lower.includes("reset to continue") || lower.includes("pn:") || lower.includes("error:9");
}

function errorMessage(err: unknown): string {
  return err instanceof Error ? err.message : String(err);
}

function isZeroDelta(delta: JogDelta): boolean {
  return delta.x === 0 && delta.y === 0 && delta.z === 0;
}

function getFocusableElements(root: HTMLElement): HTMLElement[] {
  const selector = [
    "button:not([disabled])",
    "input:not([disabled])",
    "select:not([disabled])",
    "textarea:not([disabled])",
    "a[href]",
    "[tabindex]:not([tabindex='-1'])",
  ].join(",");
  return Array.from(root.querySelectorAll<HTMLElement>(selector))
    .filter((element) => element.getAttribute("aria-hidden") !== "true");
}

function buttonStateStyle(base: React.CSSProperties, disabled: boolean): React.CSSProperties {
  if (!disabled) return base;
  return {
    ...base,
    opacity: 0.45,
    cursor: "not-allowed",
  };
}

function unique(items: string[]): string[] {
  const seen = new Set<string>();
  return items.filter((item) => {
    if (!item || seen.has(item)) return false;
    seen.add(item);
    return true;
  });
}

function allInstrumentPositionsReady(
  instruments: string[],
  positions: Record<string, CapturedPosition>,
  referenceInstrument: string,
  lowestInstrument: string,
): boolean {
  const required = unique([referenceInstrument, lowestInstrument, ...instruments]);
  return required.length > 0 && required.every((name) => !!positions[name]);
}

function stepLabels(isMulti: boolean): string[] {
  return isMulti
    ? ["Prepare", "Home", "XY origin", "Block height", "Z reference", "Instruments", "Save"]
    : ["Prepare", "Home", "Reference height", "Set origin", "Save"];
}

const overlayStyle: React.CSSProperties = {
  position: "fixed",
  inset: 0,
  background: theme.chrome.backdrop,
  zIndex: 50,
  display: "flex",
  alignItems: "center",
  justifyContent: "center",
  padding: 24,
};

const modalStyle: React.CSSProperties = {
  width: "min(920px, 96vw)",
  maxHeight: "92vh",
  background: theme.color.surface,
  border: `1px solid ${theme.color.border}`,
  borderRadius: theme.radius.lg,
  boxShadow: theme.shadow.overlay,
  display: "flex",
  flexDirection: "column",
  overflow: "hidden",
};

const headerStyle: React.CSSProperties = {
  display: "flex",
  justifyContent: "space-between",
  alignItems: "flex-start",
  gap: 12,
  padding: "16px 18px",
  borderBottom: `1px solid ${theme.color.border}`,
};

const headerActionsStyle: React.CSSProperties = {
  display: "flex",
  alignItems: "center",
  gap: 8,
  flexShrink: 0,
};

const bodyStyle: React.CSSProperties = {
  display: "grid",
  gridTemplateColumns: "minmax(140px, 170px) minmax(0, 1fr)",
  minHeight: 0,
  overflow: "hidden",
};

const stepsStyle: React.CSSProperties = {
  padding: 12,
  borderRight: `1px solid ${theme.color.border}`,
  background: theme.color.surfaceMuted,
  display: "flex",
  flexDirection: "column",
  gap: 6,
};

const contentStyle: React.CSSProperties = {
  padding: 18,
  overflow: "auto",
};

const stepButtonStyle: React.CSSProperties = {
  display: "flex",
  alignItems: "center",
  gap: 8,
  border: "1px solid transparent",
  background: "transparent",
  color: theme.color.textFaint,
  borderRadius: theme.radius.sm,
  padding: "8px 9px",
  fontSize: 12,
  textAlign: "left",
  cursor: "default",
};

const activeStepStyle: React.CSSProperties = {
  ...stepButtonStyle,
  background: theme.color.surface,
  border: `1px solid ${theme.color.accentTintBorder}`,
  color: theme.color.accentText,
  fontWeight: 600,
};

const completedStepStyle: React.CSSProperties = {
  ...stepButtonStyle,
  color: theme.color.successText,
  background: theme.color.successBg,
  border: `1px solid ${theme.color.successBorder}`,
};

const stepNumberStyle: React.CSSProperties = {
  width: 20,
  height: 20,
  borderRadius: "50%",
  background: theme.color.border,
  color: theme.color.ink,
  display: "inline-flex",
  alignItems: "center",
  justifyContent: "center",
  fontSize: 11,
  flexShrink: 0,
};

const sectionTitleStyle: React.CSSProperties = {
  ...theme.panelTitle,
  margin: "0 0 10px",
  fontSize: 16,
};

const instructionStyle: React.CSSProperties = {
  margin: "0 0 12px",
  color: theme.color.textSecondary,
  fontSize: 13,
  lineHeight: 1.45,
};

const fieldRowStyle: React.CSSProperties = {
  display: "grid",
  gridTemplateColumns: "repeat(auto-fit, minmax(160px, 1fr))",
  gap: 10,
  marginBottom: 12,
};

const fieldStyle: React.CSSProperties = {
  display: "flex",
  flexDirection: "column",
  gap: 3,
  minWidth: 0,
};

const labelStyle: React.CSSProperties = {
  ...theme.fieldLabel,
};

const inputStyle: React.CSSProperties = {
  ...theme.input,
  minWidth: 0,
};

const smallInputStyle: React.CSSProperties = {
  ...inputStyle,
  ...theme.mono,
  width: 58,
  padding: "4px 6px",
  fontSize: 12,
};

const actionRowStyle: React.CSSProperties = {
  display: "flex",
  alignItems: "center",
  gap: 8,
  flexWrap: "wrap",
  marginTop: 12,
};

const buttonStyle: React.CSSProperties = {
  ...theme.btn.secondary,
  ...theme.btnSmall,
};

const primaryButtonStyle: React.CSSProperties = {
  ...theme.btn.primary,
};

const closeButtonStyle: React.CSSProperties = {
  background: "transparent",
  border: `1px solid ${theme.color.borderStrong}`,
  borderRadius: theme.radius.sm,
  color: theme.color.textMuted,
  cursor: "pointer",
  fontSize: 18,
  lineHeight: 1,
  width: 28,
  height: 28,
};

const errorStyle: React.CSSProperties = {
  ...theme.notice.error,
  marginBottom: 10,
};

const alarmStyle: React.CSSProperties = {
  ...theme.notice.error,
  border: `1px solid ${theme.color.danger}`,
  marginBottom: 10,
  display: "flex",
  alignItems: "center",
  gap: 8,
  flexWrap: "wrap",
};

const softLimitNoticeStyle: React.CSSProperties = {
  ...theme.notice.warning,
  fontSize: 11,
  marginBottom: 10,
};

const alarmTitleStyle: React.CSSProperties = {
  color: theme.color.danger,
  fontWeight: 700,
  letterSpacing: "0.04em",
};

const alarmButtonStyle: React.CSSProperties = {
  ...theme.btn.danger,
  ...theme.btnSmall,
  marginLeft: "auto",
};

const noteStyle: React.CSSProperties = {
  ...theme.notice.info,
  marginBottom: 10,
};

const noInstrumentsStyle: React.CSSProperties = {
  ...theme.notice.warning,
  marginBottom: 10,
};

const busyStyle: React.CSSProperties = {
  ...theme.notice.warning,
  marginBottom: 10,
};

const jogPanelStyle: React.CSSProperties = {
  display: "flex",
  alignItems: "center",
  gap: 18,
  border: `1px solid ${theme.color.border}`,
  borderRadius: theme.radius.md,
  padding: 12,
  width: "fit-content",
  maxWidth: "100%",
  flexWrap: "wrap",
};

const dpadStyle: React.CSSProperties = {
  display: "grid",
  gridTemplateColumns: "repeat(3, 40px)",
  gridTemplateRows: "repeat(3, 40px)",
  gap: 2,
};

const zPadStyle: React.CSSProperties = {
  display: "flex",
  flexDirection: "column",
  gap: 2,
};

const jogButtonStyle: React.CSSProperties = {
  width: 40,
  height: 40,
  display: "flex",
  alignItems: "center",
  justifyContent: "center",
  background: theme.color.surface,
  border: `1px solid ${theme.color.borderStrong}`,
  borderRadius: theme.radius.md,
  color: theme.color.text,
  fontWeight: 600,
  cursor: "pointer",
};

const padCenterStyle: React.CSSProperties = {
  display: "flex",
  alignItems: "center",
  justifyContent: "center",
  color: theme.color.textFaint,
  fontSize: 10,
};

const stepFieldsStyle: React.CSSProperties = {
  display: "flex",
  flexDirection: "column",
  gap: 6,
};

const stepFieldStyle: React.CSSProperties = {
  display: "flex",
  alignItems: "center",
  gap: 6,
};

const stepHintStyle: React.CSSProperties = {
  color: theme.color.danger,
  fontSize: 11,
};

const instrumentListStyle: React.CSSProperties = {
  display: "grid",
  gridTemplateColumns: "repeat(auto-fit, minmax(180px, 1fr))",
  gap: 8,
  marginBottom: 12,
};

const instrumentRowStyle: React.CSSProperties = {
  border: `1px solid ${theme.color.border}`,
  borderRadius: theme.radius.sm,
  padding: "7px 9px",
  display: "flex",
  justifyContent: "space-between",
  gap: 8,
  fontSize: 12,
  color: theme.color.text,
};

const activeInstrumentStyle: React.CSSProperties = {
  border: `1px solid ${theme.color.border}`,
  borderRadius: theme.radius.md,
  padding: 12,
  background: theme.color.surfaceMuted,
};
