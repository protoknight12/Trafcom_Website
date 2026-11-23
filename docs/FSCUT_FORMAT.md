# FSCUT / CypCutE nesting files (.lxd, .lxds)

What `fscut.py` knows about the files the laser's CypCutE (FSCUT controller software) saves for a nest ("разкрой").
Worked out from our own files on 2026-11-23 (CypCutE 6.4.2330.6 and 10.2501.4.0): nests (a sheet with parts) and
single parts, in both formats. The sample files themselves are
machine files and stay out of git (`machine_data/fscut_samples/`, gitignored).

Both formats carry the same things: the cutting technology, the parts with their quantities, the sheets, and one or
more nest results (the placed parts on a sheet). **Neither stores the cut length or the cutting time** - CypCut
computes them on screen; `fscut.py` computes length and pierces from the geometry.

## Old format `.lxd`

One plain UTF-8 XML file, root `<LXDDocument>`:

- `DocHeader` - `HomeRef`.
- `Technical/Channels/Channel[ChannelPort]/CamParams` - see Technology below; a free-text `Note`.
- `Blocks/Block[Name]` - `BasePoint`, `Entities` with the part's geometry:
  `Circle[Radius]/Center[X,Y]`, `LwPolyline[PolyFlag]/Point[X,Y,Bulge]` (PolyFlag bit 1 = closed), `LeadIn`,
  `MicroConnects`. Missing attributes are 0 (`<Point/>` is the origin).
- `NestParts/NestPart[Name, Amount, AmountUsed]/Insert` - `InsertPoint`, `BlockName`, scales.
- `NestPlates/NestPlate[Amount, AmountUsed]/LwPolyline` - the sheet rectangle.
- `NestResults/NestResult[Name, Utilization]/Entities` - the placed geometry already flattened into plain entities
  (no reference to which part each one is - the old format cannot say how many of each part are on the sheet).
  The sheet outline is the `LwPolyline` with a `NestPlate` attribute; a 2-point polyline across the sheet is a
  cut-off line and is cut.

## New format `.lxds`

A ZIP (store or deflate, no password) of XML parts plus two binary files:

| Part | Content |
|---|---|
| `info.xml` | `Application[AppName, AppVer]`, `SavedBy[SaveTime]` |
| `Technical/content.xml` | `Technical[MaterialName, Thickness]` + channels as in the old format |
| `Nest2D/Parts/content.xml` | `NestPart[Handle, Name, Amount, AmountUsed, AmountDone]` |
| `Nest2D/Plates/content.xml` | `NestPlate[Handle, Amount, AmountUsed]/LwPolyline/Point` (sheet size in XML) |
| `Nest2D/Results/content.xml` | `NestResult[Index, Name, PartCount, Utilization, SizeX, SizeY]` |
| `Nest2D/Results/<Index>/content.xml` | `NestResult[PlateAmount, Utilization]/Shapes/Shape`: the sheet (`NestPlate=`), one `Insert` per placed part (`NestPart=` handle, `BlockName` = a `{GUID}` block), remnant outline (`LayerName="Remnant"`) |
| `Nest2D/Results/<Index>/thumbnail.emf` | CypCut's own preview (EMF) |
| `Blocks/content.xml` | `Block[Name]/Shapes/Shape[Handle]`. The `{GUID}` blocks of a result hold the part **already placed** (sheet coordinates, empty `BasePoint`) |
| `Shapes2D/content.xml` | per shape `Handle`: element = class (`Circle`, `Arc`, `Line`, `LwPolyline`, `PolyBezier`), `LayerName`, `GeoAddr` (offset into `Geometry2D/data.bin`), lead-ins, micro-joints |
| `Geometry2D/data.bin` | the coordinates (below) |
| `Shapes2D/data.bin` | per-shape attributes, not needed for drawing |

Part names are UTF-8 (Cyrillic reads fine); CypCut's material names are Chinese (`碳钢` black steel, `不锈钢`
stainless, `铝` aluminium) - `fscut.MATERIAL_NAMES`.

### `Geometry2D/data.bin`

Little-endian, starts with `BCMP`. A record at `GeoAddr` (8-aligned):

```
d1d1d1d1  u32 0  u64 size     'S' 00  u16 n  "TGks<Class>"   pad to 8
c1c1c1c1  u32 1  u64 size     'S' 00  u16 n  "<Class>"       pad to 8
payload
c0c0c0c0  d0d0d0d0
```

A point inside a payload is `10 00 02 00` followed by two doubles (x, y).

| Class | Payload |
|---|---|
| `Line` | point start, point end |
| `Circle` | point centre, double r, u32 0, two doubles (not used) |
| `Arc` | point centre, double r, u32 0, double start, double end (radians, CCW) |
| `LwPolyline` | double ?, u32 flags (bit 1 = closed), u32 count, count x (u32 index, double x, double y, double bulge, u32 0) - bulge as in DXF |
| `PolyBezier` | cubic segments, 4 control points each (points tagged as above) |

Checked against the DXF CypCut exported from the same nest: every block's cut length agreed to 0.01 % (Bezier parts
0.2 %, flattening), the drawing matches `thumbnail.emf`.

## Part files (one detail, no nest)

FSCUT also saves a single part (a "парт") in both formats - same container, no nest result:

- old `.lxd`: the part is the document's own top-level `<Entities>` (next to empty `NestResults`).
- new `.lxds`: `Model2D/content.xml` = `Model2D[Name]` with `ExtMin`/`ExtMax` and `Shapes/Shape[Class, Handle,
  GeoAddr]` straight into `Geometry2D/data.bin`; `Nest2D/Plates` and `Results` are empty. (`Nest2D/Parts` may still
  list the parts of the drawing the part came from.)

`fscut.parse()` returns it as `model` (name, shapes, size, cut length, pierces). The computed size matched CypCut's
own `ExtMin`/`ExtMax` exactly and the length the DXF of the same part.

## Technology (both formats)

`CamParams[WorkSpeed]` is mm/s (x 60 = mm/min). `Cut[GasType, GasPressure (bar), PeakPower (W), Focus (mm),
Height (mm)]`. `GasType` 1 = air, 2 = O2, 3 = N2 (4/5/6 = the same at high pressure). `Pierce1[TimeFollow,
TimeStay]` in ms - their sum is used as the pierce time. Channels above 16 (101, 102, 111, 121) are service channels
(rapid moves, marking) and are ignored. More on the channels: the eckert project's `docs/14_plct_converter.md`.

Other groups seen in our files (kept whole, not as fields): `LeadLineParam`, `LayerCutParams`, `CornerParams`,
`CutHeadParams`, `SuperCut`, `SmartEndTech` (+ `ExtineLaser`, `InnerLaser`), `SlowLeadHead`/`SlowLeadExt`,
`SlowEndHead`/`SlowEndExt`, `MicroSpeedParams`, `CutModeTypeParam`, `PwmFreqFunc`/`PwmRatioFunc` (power curves as
`Knot` points), `MaterialParams[MaterialName, NozzleSpec, Thickness]` (the nozzle). CamParams itself carries slow
lead/end, flash-cut, cut gap, compensation and cool-down values.

What a machine's technology stores (`MachineCutTechnology`, key Вид + thickness + gas + nozzle): speed, pierce time
(sum of `TimeFollow + TimeStay` over the used pierce steps), the fields of `fscut.CUT_FIELDS` (pressure, peak power,
current %, PWM frequency, duty % = `PwmRatio` x 100, focus, height, beam size, lift height), `PierceStepCount` and
`fscut.STEP_FIELDS` for pierce steps 1-3, and the channel's whole tree as `[path, attributes, text]` rows
(`fscut._raw()`, `params_json`). A field the file does not carry stays empty (older CypCut versions lack
`PeakPower`, `BeamSize`, ...), never 0.

## Not done yet

A converter between the two formats (owner, 2026-11-23: later). Lead-ins/micro-joints are read by CypCut but not drawn
or counted here.
