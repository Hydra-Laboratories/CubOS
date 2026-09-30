# Color-Matching Demo View

The color-matching demo view turns a saved or running CubOS campaign into a
presentation for an audience. It shows the selected target, each eligible well
measurement, the best result so far, recipes, and a replay timeline without
exposing hardware controls.

Start and manage the experiment in the regular Operator interface. After the
campaign exists, open its demo view with:

```text
http://<cubos-host>:8742/?view=demo&campaign=<campaign-id>
```

For a clean browser source in OBS or another recorder, add `overlay=1`:

```text
http://<cubos-host>:8742/?view=demo&campaign=<campaign-id>&overlay=1
```

## Live and recorded presentation

With no local video selected, the page follows the campaign data from CubOS.
New accepted measurements appear as the experiment progresses, and “best so
far” updates only when a scientifically eligible result improves the score.

For an edited replay, select a local camera recording in the demo view. Files
up to 512 MB are supported. The video stays in the browser on the computer
that selected it; CubOS does not upload or archive the video.

Use **Add sync marker** to align a recognizable instant in the recording with
the campaign timeline. A marker stores an alignment anchor, not proof that the
camera and CubOS clocks were synchronized. The offset follows:

```text
video time = campaign elapsed time + offset
```

For example, if the event at 30 seconds in the campaign appears at 40 seconds
in the video, the offset is `+10 seconds`. Marker records include the local
recording identity and the selected video and campaign times so the alignment
can be reproduced. Adding a marker never moves hardware or changes experiment
state.

## What the view reports

A result is eligible for “best so far” only when the protocol run completed,
the color measurement and comparison were accepted, its score is finite, and
its image-processing profile matches the target where required. Rejected,
partial, incompatible, and missing measurements remain visible as unavailable
evidence; they are not silently scored.

Replay reveals an attempt only after its own saved completion event, so moving
backward on the timeline does not expose future wells or scores. If a browser
cannot display a camera TIFF directly, CubOS serves a lossless PNG rendition
for the page while preserving the original TIFF unchanged.

## Evidence export

The download contains a versioned manifest, campaign and run configuration
snapshots, measurements, events, target evidence, raw and annotated images,
sync markers, and SHA-256 hashes. Missing or partial evidence is listed
explicitly. The local camera video is separate from this ZIP; keep it beside
the export if you want to reproduce a synchronized replay.

The camera colors and Delta E values are estimates produced by the configured
acquisition and processing setup. The demo view does not guarantee scientific
color accuracy or replace calibration, controlled lighting, or validation of
the measurement method.

