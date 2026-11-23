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
| `Shapes2D/data.bin` | per-shape cutting attributes at the shape's `DataAddr` (lead-in, compensation - below) |

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

### Lead-in, micro-joints, compensation

Worked out from one part the owner saved from CypCut as both `.lxd` and `.lxds` with these set (2026-11-23).

Old `.lxd` - children of the `Circle` / `LwPolyline`, in this order:

```
<LwPolyline PathStartParam="1.5" PointCount="4" PolyFlag="1">
  <LeadIn LeadType="1" Pos="1.5" Angle="1.57079633" Length="4" ArcRadius="2" LeadHoleR=".5"/>
  <ToolCompensation Style="1" Size=".4"/>          (empty = no compensation)
  <MicroConnects><MicroConnect Pos="2" Size=".4"/></MicroConnects>
  <Point .../>
```

`IsFill="true"` on the contour (new format: the first u32 of `Curve` = 1) marks the OUTER contour; without it CypCut
treats the outer contour as a hole and puts its lead-in and compensation on the wrong side (owner's test 2026-11-23).
The writers keep it from the source; when no contour has it (a DXF) every closed contour whose box lies inside no other
contour's box gets it. `Pos` = segment index + fraction along the polyline's own vertices (0 is left out); `PathStartParam` = the
lead-in's `Pos`. A circle has no `Pos` - its start is the `StartAngle` attribute (radians). Older files have a short
`<LeadIn Pos Angle>` with no length (CypCut then uses its defaults).

New `.lxds` - the shape's record in `Shapes2D/data.bin` (same `d1`/`c1`/`c0`/`d0` framing as Geometry2D, padding
relative to the record start) holds sub-records `Object`, `Shape`, `LeadParam` (only when there is a lead-in) and
`Curve`. `LeadParam` payload: u32 LeadType, 12 bytes, 5 doubles Pos, Angle, Length, ArcRadius, LeadHoleR. `Curve`
payload: 8 bytes, u32 (1 = filled), u32 compensation Style (0 = none), double Size. Micro-joints stay in XML
(`Shapes2D/content.xml`, `MicroConnects` under the shape; each `MicroConnect` may hold its own `LeadIn`, which the old
format does not have and is not written). A circle's start angle is the double after its radius in Geometry2D.

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

## Writing (`fscut.write_lxd()`)

Step 1 (owner's decision 2026-11-23): one part as an old `.lxd`, mirroring a real old part file - `DocHeader`
(ExtMin/ExtMax/HomeRef, HandleSeed), `Technical/Channels` (an empty `Channel ParamFlag="256"`, port 1 with the
technology, empty ports 2..15), Viewports, empty TextStyles/Layers/Blocks, `Entities`, empty Nest* lists; CRLF line
ends. Geometry: `Circle` and closed `LwPolyline` only (the two entity forms seen in real old files) - lines/arcs are
chained end to start (`fscut.chain()`, 0.01 mm) with DXF bulges. Technology: the stored tree (`params_json`) rebuilt,
then the speed, gas and every field written over it. Round trip checked on our files (parts, a nest's geometry, a
DXF through `analyze_dxf_geometry()`): same size, cut length, pierces and technology fields.

The owner opened the first files in CypCut (2026-11-23): they open, but had no lead-ins / micro-joints /
compensation. Since then a part file's contours (`parse()['model']['entities']`: every Circle / LwPolyline exactly as
stored, with its `LeadIn` / `ToolCompensation` / `MicroConnects`, `StartAngle`, `PathStartParam`) are written as they
are; only the other shapes (`model['loose']`: lines, arcs, Beziers) are chained. Nothing is generated (owner's
decision): a DXF still gets none - CypCut adds them. Checked: `цом.lxds` converted gives the same 27 elements as the
`цом.lxd` CypCut saved from the same part. The owner opened such a converted file in CypCut (2026-11-23): lead-ins,
micro-joints and compensation are there.

## Writing the new format (`fscut.write_lxds()`)

Step 2 (2026-11-23, after the `.lxd` files worked): one part as a new-format part file, mirroring the part CypCutE
6.4.2330.6 saved (`DocType="Model2D"`, the ZIP entries in the same order, stored, with directory entries). Each
contour = one `Geometry2D/data.bin` record (Circle: point, r, u32 0, start angle, 0; LwPolyline: double
PathStartParam, u32 closed, u32 count, vertices, u32 0) + one `Shapes2D/data.bin` record (`Object` = u32 handle,
`Shape` = u32 1 + 56 zeros, u32 lead count, [`LeadParam`], u32 0, `Curve`). A sub-record's u64 size counts from its
`c1` to its `c0`, a record's from `d1` to `d0`; both files start `BCMP`, u32 file length, u32 1, zeros to 4096.
`Geometry2D/content.xml` and `Shapes2D/content.xml` carry the upper-case MD5 of their `data.bin`. Micro-joints go into
`Shapes2D/content.xml` with an empty `<LeadIn/>` under each `MicroConnect`, as CypCut writes them. Written from
CypCut's own part, both binary files came out byte for byte the same as CypCut's (only the handles differ).

Not written: `thumbnail.emf`, `GalvoParams` / `TechFlags` in Technical (CypCut uses its defaults), nests.
The owner opened `цом_от_lxd.lxds` and `osnova_от_dxf.lxds` in CypCut (2026-11-23, after the IsFill fix): they work.

## Converting nests (`fscut.write_lxd_nest()` / `write_lxds_nest()`)

Owner's request 2026-11-23, both ways, no reference pair (written from `garn_copy.lxd` and the jeep `.lxds`).
`parse()` now also gives each part its `pieces` ([(entity | None, shapes)] - entity = Circle / LwPolyline with its
cutting attributes) and each result `placed` ([{'part': index, 'pieces'}]), `extra` (cut-off lines and other pieces
outside the parts) and `plate_outline`. New format: one placed part = one result Insert ({GUID} block, already in
sheet coordinates). Old format: the result keeps only flattened contours, so they are grouped - every outer contour
(IsFill, or by containment when none is marked) + everything inside its box = one placed part, matched to a NestPart
by contour count + cut length (`_sig()`, turned parts match too). This also gives the old format its counts per part.

Written: old - one `Block` (`PartN`, BasePoint = the part's lower left, OutlineID = its outer contour) + `NestPart`
(Insert of it) per part, `NestPlates`, and per result a `NestResult` (`Flags="704"`) whose Entities are the placed
contours + the extra ones + the sheet outline (`NestPlate=`), then ExtMin / ExtMax / HomeRef. New - per part a
`{GUID}` block (Flags 512) + `NestPart` (Insert + Shape + ExtShapes), per sheet a `NestPlate` (inline LwPolyline +
Shape), per result `Nest2D/Results/<n>/content.xml` (sheet outline Shape + one Insert Shape per placed part, each in
its own `{GUID}` block without Flags) + the summary line in `Nest2D/Results/content.xml`; `content.xml`'s
`ActiveModelID` = the first result's ID, no `Model2D`. A placed part that matches no NestPart becomes its own part.
Checked on both samples, both ways: same sheet, counts per part, cut length, pierces, lead-ins, micro-joints and one
outer contour per placed part. The owner opened `garn_разкрой_от_lxd.lxds` and `jeep_разкрой_от_lxds.lxd` in CypCut
(2026-11-23): they work.

One part of a nest (`fscut.nest_parts()`, the same parts the converters write): downloaded as an FSCUT part (`.lxd` /
`.lxds`, with its lead-in / micro-joints / compensation and the nest's technology) or a DXF, or made a catalog Detail
- see `view_detail_nest_part()` / `view_detail_nest_part_detail()` in `app.py`.

Not written: remnants (`LayerName="Remnant"`), thumbnails. An old short lead-in (`<LeadIn Pos Angle>` with no
length) goes into the new format as LeadType 0 / length 0. Lead-ins/micro-joints are not drawn or counted on the page.
