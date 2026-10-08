/** Browser-side recording for the simulation canvas.
 *
 * The caller owns the simulation clock. `onFrame` is called with logical run
 * time in seconds, so recording does not duplicate or alter the playback
 * state machine used by the viewer.
 */

export type CanvasRecordingOptions = {
  canvas: HTMLCanvasElement
  duration: number
  playbackRate?: number
  fps?: number
  finalHoldMs?: number
  onStart?: () => void
  onFrame: (time: number) => void
  onEnd?: () => void
}

export type RecordingExport = {
  id: string
  gif_url: string
  mp4_url: string
  gif_filename: string
  mp4_filename: string
}

const nextFrame = () => new Promise<void>(resolve => requestAnimationFrame(() => resolve()))

function supportedMimeType() {
  const candidates = [
    'video/webm;codecs=vp9',
    'video/webm;codecs=vp8',
    'video/webm',
  ]
  return candidates.find(type => MediaRecorder.isTypeSupported(type)) ?? ''
}

/** Record only `canvas` from time zero through the end of a run. */
export async function recordCanvasAnimation({
  canvas,
  duration,
  playbackRate = 24,
  fps = 24,
  finalHoldMs = 1000,
  onStart,
  onFrame,
  onEnd,
}: CanvasRecordingOptions): Promise<Blob> {
  if (!canvas.captureStream) throw new Error('This browser cannot capture the simulation canvas')
  if (typeof MediaRecorder === 'undefined') throw new Error('This browser cannot record the simulation canvas')
  if (!Number.isFinite(duration) || duration < 0) throw new Error('A finite run duration is required')
  if (!Number.isFinite(playbackRate) || playbackRate <= 0) throw new Error('Playback rate must be positive')

  const mimeType = supportedMimeType()
  if (!mimeType) throw new Error('This browser cannot encode a WebM recording')
  const stream = canvas.captureStream(fps)
  const recorder = new MediaRecorder(stream, {mimeType})
  const chunks: BlobPart[] = []
  recorder.addEventListener('dataavailable', event => {
    if (event.data.size) chunks.push(event.data)
  })

  const stopped = new Promise<void>((resolve, reject) => {
    recorder.addEventListener('stop', () => resolve(), {once: true})
    recorder.addEventListener('error', () => reject(new Error('Canvas recording failed')), {once: true})
  })

  onStart?.()
  onFrame(0)
  // Give React/Three.js one paint to commit the reset state before recording.
  await nextFrame()
  recorder.start()

  const startedAt = performance.now()
  await new Promise<void>(resolve => {
    const tick = (now: number) => {
      const logicalTime = Math.min(duration, (now - startedAt) / 1000 * playbackRate)
      onFrame(logicalTime)
      // The logical simulation time is the completion gate. This still
      // finishes correctly when requestAnimationFrame is throttled in a
      // background tab, instead of truncating at a wall-clock timeout.
      if (logicalTime >= duration) resolve()
      else requestAnimationFrame(tick)
    }
    requestAnimationFrame(tick)
  })
  onFrame(duration)
  // Keep the completed frame visible long enough to read in the exported GIF.
  await new Promise<void>(resolve => window.setTimeout(resolve, Math.max(0, finalHoldMs)))
  recorder.stop()
  await stopped
  stream.getTracks().forEach(track => track.stop())
  onEnd?.()
  return new Blob(chunks, {type: mimeType})
}

/** Send an in-memory WebM to the localhost simulation exporter. */
export async function uploadCanvasRecording(blob: Blob, endpoint = '/api/twin/recording'): Promise<RecordingExport> {
  const response = await fetch(endpoint, {
    method: 'POST',
    headers: {'Content-Type': blob.type || 'video/webm'},
    body: blob,
  })
  const data = await response.json().catch(() => ({}))
  if (!response.ok) {
    const detail = typeof data.detail === 'string' ? data.detail : 'Recording export failed'
    throw new Error(detail)
  }
  return data as RecordingExport
}
