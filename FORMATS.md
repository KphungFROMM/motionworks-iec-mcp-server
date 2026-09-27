# MotionWorks IEC export formats

Everything here was established by inspecting real exports from MotionWorks IEC 3
Pro 3.7.5.1 (`RotaryKnife_ASP_v350`, `RK_DemoOnMP2300Siec`, `RK_DemoOnMP3300iec`,
`TopCutter`). Where a claim is an inference rather than an observation it is
labelled. This file exists so the parsers can be maintained without re-deriving
the formats from scratch.

---

## 1. The three sources

A MotionWorks project can be exported three ways, and they are **complementary**:

| | Native project | PLCopen XML | Extended IEC 61131-2 |
|---|---|---|---|
| Entry point | `<Project>.mwt` + `C/ POE/ DT/ HW/ LIB/` | `<Project>.xml` | `<Project>/` |
| Code | ❌ binary (`src.st1`) | ✅ all languages | ✅ ST as text, LD/FBD as `.GE` |
| Vendor types | inferred from `.DIT` | ✅ **full library** | ❌ absent |
| Global vars | `.VGR` (binary), `.GVB` | ✅ with `AT %` + comments | ✅ with `AT %` + comments |
| Task names | ✅ `eCLRPouDependencies.dat` | ❌ priority+interval only | ✅ `.EXP` files |
| I/O config | `.IOC` (binary) | ❌ | ✅ `IOCONFIGURATION.EIO` |
| Localization | `*Translation.xml` | ❌ | ✅ |

**Practical guidance:** use the PLCopen XML export as the primary/authoritative
source, the Extended export for ST text, addresses, task names and I/O, and the
native project for the POU/task map.

---

## 2. Native project layout

```
<Project>.mwt                     compound binary (OLE2), project document
<Project>/
├── C/                            code
│   └── <Configuration>/R/Resource/    compiled artifacts + plain-text metadata
├── POE/<PouName>/                per-POU export cache
│   ├── src.st1                    ⚠ compound binary, NOT text
│   ├── tmp.sto                    ⚠ binary
│   ├── NodeProperties.xml
│   ├── pouReserve.prs
│   ├── <Pou>.CCI, <Pou>V.cfb      binary metadata
│   └── <POU>Translation.xml       UTF-16 localization table
├── DT/                           data types (`Tyllist.typ`)
├── HW/                           hardware
└── LIB/
```

`TREE.XML` and `ioconfig.IOC` are also binary.

### Plain-text files worth reading

**`NODES.LST`** — tab-separated tree, one row per node:

```
CONFIGURATION	2	Configuration		eCLR
RESOURCE	3	Resource			MP2600iec
TASKDIR	4	Tasks		eCLR	MP2600iec
TASK	5	FastTsk		eCLR	MP2600iec
PROGRAM	6	TopCutterCutControl	TopCutterCutControl	eCLR	MP2600iec
VAR_GLOBALS	4	Global_Variables	C\Configuration\R\Resource\Global_Variables.VGR
```

Fields: `kind`, `depth`, `name`, `subName` (call name), `plcType`, `procType`.

**`eCLRPouDependencies.dat`** — the POU index. This is the single most useful
native file: it alone yields every POU and its task membership.

```
PouDep.Cache;schema 1
0,CalcBezier,3,FB
4,CamGenerator,3,FB; 2,CalcSpline; 0,CalcBezier; 3,MasterIndex_Lookup
7,TopCutterCutControl,2,PG
16,FastTsk,1,TA; 7,TopCutterCutControl
```

Format: `idx,name,code,kind[; depIdx,depName]...`

* `kind` codes, confirmed by the trailing two-letter tag: `1`=`TA` (task),
  `2`=`PG` (program), `3`=`FB` (function block). (`FC`, `CF`, `RS` are inferred from
  the naming convention and have not been observed.)
* The first line is a schema banner, **not** a row — it has only 2 comma fields.
* Dependency entries are `idx,name` pairs separated by `; `.
* **For a `TA` row the dependencies are its programs.** This is how task
  membership is recovered without the IDE.

**`OCIRES.INI`** — processor identity: `ProcessorType=MP2600iec`,
`ResourceProcType=eCLR`.

**`<Task>.SET`** — IEC task configuration, e.g. `FastTsk.SET`:

```
TASK FastTsk
(TYPE := CYCLIC,
INTERVAL := T#4ms,
PRIORITY := 0,
WATCHDOG := 4
WATCHDOG_ENABLED := YES
);
```

**`Resource.set`** — `RESOURCE  COMPORT: DLL plc\socomm.dll -ip192.168.1.200 -p41100 -TO2000`.

`eCLRPouInfo.pil` is compressed and not parsed.

---

## 3. Extended IEC 61131-2 export

```
<Project>/
├── <Pou>.ST      Structured Text     (TYPE: POU, IEC_LANGUAGE: ST)
├── <Pou>.GE      LD or FBD worksheet (TYPE: POU, IEC_LANGUAGE: LD|FBD)
├── <Pou>.IEC     data types          (TYPE: DATA_TYPE)
├── <Pou>.DIT     POU interface metadata (T: FUNCTION_BLOCK <Name>)
├── PHYSHARDWARE.EXP
└── Configuration|Resource|R/Resource/
    ├── Global_Variables.GVB
    ├── IOCONFIGURATION.EIO
    ├── RESOURCE.EXP  CONFIGURATION.EXP
    ├── FastTsk.EXP MedTsk.EXP SlowTsk.EXP BG.EXP Start.EXP
    └── *Translation.xml
```

Note the resource folder is `Resource/` in some exports and
`R/Resource/` in others, and `Configuration/` appears both at the top level and
nested — so locate files by name, not by a fixed relative path.

### 3.1 POU file header

Every `.ST`/`.GE`/`.IEC` file begins with a properties block:

```
(*@PROPERTIES_EX@
TYPE: POU
LOCALE: 0
IEC_LANGUAGE: ST
PLC_TYPE: independent
PROC_TYPE: independent
*)
(*@KEY@:DESCRIPTION*)

(*@KEY@:END_DESCRIPTION*)
PROGRAM TopCutterCutControl

(*Group:Default*)
VAR ... END_VAR
...
```

* `TYPE` is `POU` or `DATA_TYPE`.
* `IEC_LANGUAGE` is authoritative for the body language: `ST`, `LD` or `FBD`.
  A `.GE` file holds **both** LD and FBD — only this field distinguishes them.
* Closing keyword is `END_PROGRAM` for programs and `END_FUNCTION_BLOCK` for
  function blocks; both appear in the sample set.

### 3.2 `(*@KEY@: ... *)` regions

| Marker | Meaning |
|---|---|
| `DESCRIPTION` / `END_DESCRIPTION` | free-text POU documentation |
| `WORKSHEET` / `END_WORKSHEET` | the graphical body (LD/FBD only) |
| `RESOURCE`, `TASK`, `PGINSTANCE` | resource/task config in `.EXP` files |
| `FILE 'name'` | an embedded settings file |
| `INCLUDE_GLOVAR`, `INCLUDE_IOC` | references to the global-variable and I/O files |

### 3.3 Declaration blocks

Ordinary IEC: `VAR`, `VAR_INPUT`, `VAR_OUTPUT`, `VAR_IN_OUT`, `VAR_EXTERNAL`,
`VAR_GLOBAL`, `VAR_TEMP`, `VAR_RETAIN`, terminated by `END_VAR`. Addresses use
`AT %…`:

```
VAR_GLOBAL
    PLCMODE_RUN	AT %MX1.7.0 :	BOOL;(**)
    PLC_SYS_TICK_CNT	AT %MD1.0 :	DINT;
    PLC_TASK_1	AT %MB1.5000 :	TASK_INFO_ECLR;
END_VAR
```

`(*Group:<name>*)` headings precede a block and serve as its documentation.
Inline `(* ... *)` after a declaration is its per-variable comment; a bare `(**)`
carries no information.

### 3.7 `.DIT` — function-block interfaces (the vendor library's only written form)

````
(*
T: FUNCTION_BLOCK MC_Power
FW: YES	NO_VARIANT MC_Power SKIP_NOTHING	SFB_INIT_RELEASE
CI#: 78
QSL: 0
QVE: 10
QPar: 10
QFBI: 0
*)
@V 1 1 0
Axis	1	VAR_IN_OUT	@TYP:24
;
@V 1 2 0
Enable	2	VAR_INPUT	@TYP:1
;
````

Rows are `name <tab> slot <tab> scope <tab> @TYP:<id>`. This is the **only** place
the firmware motion library's formal parameters are written down — no export defines
`MC_Power` as a type, and the PLCopen XML only *references* it by name.
**25 blocks** with full interfaces were recovered this way from a project that
retains these files (see the caveat below).

Three things about the `@TYP:<id>` scheme, all established by correlating every
`.DIT` against the variables the project itself declares:

* **Small ids (1, 3, 4, 7, 11) are builtins and consistent across files.** Learned
  with no contradictions across three projects: `1 = BOOL` (171 confirmations),
  `3 = INT` (44), `4 = DINT` (10), `7 = UINT` (11), `11 = LREAL` (5).
* **Ids ≥ 1024 are registered types and appear in `TYLLIST.TYP`**:
  `1053 = AXIS_REF`, `1062 = Y_MS_CAM_STRUCT`, `1306 = CamSegmentStruct`.
* **A registered type referenced by a *small* id is a per-file local id and is not
  resolvable from any table.** `MC_Power`'s `Axis` is `@TYP:24` while
  `TopCutterCutControl`'s `AXIS_REF` is `@TYP:1053`. Guessing here would produce
  confidently wrong types, so unresolved ids are reported as `@TYP:<n>` and listed
  as a divergence. Where the project actually calls the block, the pin's type can
  instead be *inferred* from the variable driving it, and those pins are flagged
  `typeInferred`.

> **Caveat:** `.DIT` files are compile *outputs*. Only a project whose native folder
> still contains `C/Configuration/R/Resource/` has them. Of the four sample projects,
> `TopCutter` has 82; the other three have only `TYLLIST.TYP` and no `.DIT` at all.
> For those, the firmware library reference (section 4) supplies the interface, which
> is why it matters so much.

### 3.8 `TYLLIST.TYP` — the registered type-id table

```
(*
NDTE: 39
NCPE: 161
NDME: 16
*)
31 0	CTB_Types\CTB_Types	CamSegmentStruct	1306	8	USER	STRUCT
92 0	MotionBlockTypes\MotionB	Y_MS_CAM_STRUCT	1062	3	USER	STRUCT
11 0	MotionBlockTypes\MotionB	MC_BufferMode	1044	6	USER	ENUM
```

`<row> <0> \t <namespace> \t <TypeName> \t <typeId> \t <count> \t USER \t <KIND>`. Note
it contains only a handful of `MC_*` entries — the motion *blocks* are not in it, only
their supporting types.

---

## 4. The vendor library reference (outside the project)

The complete firmware function-block reference is **not** in any project export. It is
installed with MotionWorks at:

```
C:\ProgramData\Yaskawa\MotionWorks IEC 3 Pro\<version>\plc\FW_LIB\<library>\*.chm
```

24 compiled help files, one per library (`PLCopenPlus_v_2_2a`, `YMotion`,
`YCoordinatedMotion`, `YAxsGrp`, `YDeviceComm`, `YIODrv`, `YMLinkIO`, `BIT_UTIL`,
`NVTCPUDP`, `PROCONOS`, `LegacyProConOS`), plus per-library `eCLRLibraryProfiles.xml`
and `.POU` manifests. `eClrLibInfo.txt` inside a project names the DLLs it used, which
is how a project maps back to its libraries.

**Extractable, and wired into the server.** Windows' built-in help decompiler turns
each `.chm` into ordinary HTML — but *only* when driven through ShellExecute
semantics. See "How to actually decompile a CHM" below.

The pages are regular enough to parse mechanically. Shapes, located by table class
rather than by scraping prose:

| Page | Structure |
|---|---|
| Block | `<table class="fb_parameters">`; `tr` rows are either a scope banner (`td.group` = `VAR_IN_OUT`/`VAR_INPUT`/`VAR_OUTPUT`) or a pin: requirement level, name, data type, description, default. `tr.unsupported` marks a pin this firmware does not implement. |
| Data type | `<table class="fb_datatypes">`; columns `*`, `Element`, `Data Type`, `Description`, `Usage`. |
| Enumerated type | Same class; a title row then numeric value rows where the first cell *is* the value. |
| Aggregate enum index | `Enumerated_Types.htm`; rows alternate between an enum-name row and its numeric value rows. |

Verified against the installed 3.7.5.1 reference: **110 function blocks, 41 data
types, 45 enumerated values** — plus per-block `Library`, `Notes`,
`Related Function Blocks`, `Error Description` and `Example` sections.

### 4.1 How to actually decompile a CHM

**Invoking `hh.exe` directly does not work.** It exits 0 immediately and writes
nothing. Verified against four direct-spawn shapes from Python — plain `argv`,
`shell=True`, `cmd /c hh.exe`, and `CREATE_NO_WINDOW` — all of which returned 0
with zero pages, while PowerShell on the same file produced the full tree:

```powershell
# Works
Start-Process -FilePath hh.exe -ArgumentList '-decompile','<dir>','<chm>' -Wait
```

`Start-Process` uses ShellExecute semantics, which is evidently what this GUI
executable requires. `ShellExecuteExW` via ctypes was tried as a dependency-free
alternative and also produced nothing. So the server drives `hh.exe` through
PowerShell, and `MOTIONWORKS_FWLIB` pointing at pre-extracted HTML is the escape
hatch for hosts without it.

Also note `hh.exe` reports success whether or not it extracted anything, so
success must be judged by whether pages appeared.

### 4.2 Where the export and the reference disagree

They are authoritative about different things, and a discrepancy is worth keeping
rather than resolving:

* **Values.** The `MC_Direction` enum is documented in the help as four values
  (Positive_Direction, Shortest_Way, Negative_Direction, Current_Direction), while
  the PLCopen export lists five — `Both` is accepted by the firmware but absent
  from that help version. The export is authoritative for *what the firmware
  accepts*; the help is authoritative for *what a value means*.
* **Pin data types.** A project's `.DIT` records the type the compiler emitted
  (`BufferMode` as `INT`) where the block itself declares the symbolic type
  (`MC_BufferMode`). The reference is preferred for the declared type and the
  project's value is kept as `projectType`.

---

### 3.5 `IOCONFIGURATION.EIO`

```
(*YEA Input Group <Controller I/O>*)
PROGRAM IMO1 WITH FastTsk : INPUT
(
	VAR_ADR := 61440,
	END_VAR_ADR := 61440,
	DEVICE := DRIVER,
	DRIVER_NAME := 'LIODrv',
	DRIVER_PAR1 := 1,
	DATA_TYPE := BYTE
);
```

The preceding comment names the physical group (`<Controller I/O>`,
`<SGD7S> Network #1 Node #1`), which is how an axis maps to a servo node.

### 3.6 `.EXP` — task and resource configuration

```
(*@KEY@: TASK
	NAME: 'FastTsk'
	TYPE: USER_TASK
	TASKTYPE: CYCLIC
*)
	(*@KEY@: PGINSTANCE
		NAME: 'TopCutterCutControl'
	*)
	(*@KEY@: END_PGINSTANCE*)
	(*@KEY@: FILE 'FastTsk.SET'*)
TASK FastTsk
(TYPE := CYCLIC, INTERVAL := T#4ms, PRIORITY := 0, WATCHDOG := 4);
	(*@KEY@: END_FILE*)
(*@KEY@: END_TASK*)
```

`PGINSTANCE NAME:` is the program instance; `PGINSTANCE PARANAME:` (seen in
`Start.EXP`) is the parameter name and does **not** match the program name
(`PARANAME: CamGen` for `NAME: 'Initalize'`), so match on `NAME` only.

---

## 4. PLCopen XML export

Standard TC6 (`http://www.plcopen.org/xml/tc6.xsd`), produced by KW-Software
technology. Root structure:

```
<project>
  <fileHeader companyName="Yaskawa" productName="MotionWorks IEC 3 Pro"
              productVersion="3.7.5.1" creationDateTime="…"/>
  <contentHeader name="RotaryKnife_ASP_v350" version="1522791287">
    <coordinateInfo>…</coordinateInfo>
  </contentHeader>
  <types>
    <dataTypes> … project data types … </dataTypes>
    <pous>      … POU definitions …      </pous>   ← INSIDE <types>
  </types>
  <instances>
    <configurations>                          ← INSIDE <instances>
      <configuration name="Configuration">
        <resource name="Resource">
          <task priority="0" interval="00:00:00.4">
            <programInstance name="StraightCut" type="StraightCut"/>
          </task>
          <globalVars name="PLC_SYS_TICK_CNT">
            <variable name="PLCMODE_RUN" address="%MX1.7.0" comment="TRUE, if…">
              <type><BOOL/></type>
            </variable>
          </globalVars>
        </resource>
      </configuration>
    </configurations>
  </instances>
</project>
```

### Traps

1. **`<pous>` is nested inside `<types>`**, not directly under `<project>`.
   Reading it from the root silently yields zero POUs — the easiest mistake to make
   against this format.
2. **`<configurations>` is nested inside `<instances>`.**
3. **ST bodies are HTML-wrapped.** The code sits in
   `<p xmlns="http://www.w3.org/1999/xhtml" xml:space="preserve">`, with `<br/>`
   inserted and the *real newlines already present*. Unwrap by taking the text
   content and stripping markup — and do **not** re-indent, because tabs in the
   source align `:=` and trailing comments.
4. **Task names are absent.** Tasks carry only `priority` and `interval`; match
   them to the Extended export's named tasks by priority+interval.
5. **Bodies can be empty.** `TopCutter.xml` ships `<ST>` elements with no content
   for `StraightCut`, `CamGen` and `Initialize`. Treat an empty body as a gap to
   fill from another source, not as "this POU is empty".
6. **Task intervals are written oddly**: `00:00:00.4` for 4 ms and
   `00:00:00.20` for 20 ms — parse the fractional part by length, not as a
   decimal fraction of a second.

### The type library

`<pous>` (inside `<types>`) holds the vendor library: `AXIS_REF`,
`Y_ENGAGE_DATA`, `MC_TP_REF`, `CamSyncStruct`, `ProductBufferStruct`, … —
roughly 300 types, including every `MC_*` and `Y_*` the firmware provides. This is
the only source for a usable function-block signature.

---

## 5. The `.GE` worksheet format

A `.GE` body is a fixed set of sections. All 11 observed files carry the identical
13-section set, with these exact field layouts:

| Section | Arity | Fields |
|---|---|---|
| `GRA` | 7 | `width height 0 0 0 0 0` |
| `LIT` | 6 | `x1 y1 x2 y2 flags ""` |
| `TET` | 7 | `x1 y1 x2 y2 align justify text…` |
| `FBS` | 8 | `typeId pinId x y width height TypeName InstanceName` |
| `FPT` | 13 | `x1 y1 x2 y2 Name 0 0 0 dir flags 0 @ TYPE` |
| `KOT` | 8 | `x1 y1 x2 y2 0 0 varId Variable` |
| `VER` | 11 | `x y x y 1 0 1 4294967295 0 1 0` |
| `CON` | 6–8 | `a b c X1 Y1 X2 Y2` |
| `AKT` `STT` `TRT` `IBX` | 0 | empty in every observed file |

### Traps

1. **Sections are written twice.** After the row list the writer emits a second
   bare `[CON]` (or `[FPT]`) header with no rows. A parser must *accumulate* rows
   per section name; resetting on a repeated header discards half the data.
2. **Count lines are inconsistent.** `[CON]` carries two bare integers (`1` then
   `63`) before its rows; other sections carry one. Since no section's row format
   is a single integer, drop bare-integer lines wherever they appear.
3. **`[FBS]` fields 5 and 6 are a width and height**, not a corner — and the
   writer frequently emits `0 0` (all five blocks in `ServoTaskSlow.GE`).
   Treating them as a rectangle makes every block zero-sized.
4. **`[FPT]` index 8 is the direction** (`0` = input, `1` = output). Index 9 is a
   flag word (`4224`/`4225`/`4226`) and index 12 is the data type. Reading the
   direction from index 9 inverts every pin classification.
5. **`[FPT]` is grouped per block in `[FBS]` order**, and within a group the order
   is the parameter declaration order (recovered from vertical position). The
   groups are *not* globally sorted by y: in `ServoTaskSlow.GE`, `MC_Reset` is
   declared second but sits above `MC_Power`.
6. **`[TET]` entries are annotations, not values.** A block's instance-name label
   and the input/output copies of variable names are all `[TET]` rows. The pin
   list in `[FPT]` is what defines the interface.

### `[CON]` wiring

`a b c X1 Y1 X2 Y2`:

* `X1 Y1 X2 Y2` is a rectangle. When it matches an `[FPT]` pin rectangle it
  identifies that pin and, through geometry, its owning block.
* `a` selects what the *other* end is: `0` = a component record (a `[TET]`
  annotation or a bare canvas point), `1` = a pin, `2` = a block.
* `b`/`c` are ordinals. For `[TET]`+`[LIT]` they are 0-based and span both tables,
  including the block instance-name labels.

**What is confirmed:** the *structure* above (section layouts, arities, the `FBS`
width/height semantics, the `FPT` direction field, per-block grouping in `FBS`
order) is verified against every observed file. **What is not:** a complete,
reliable pin→value mapping. Reconstructing it from the `[CON]` ordinals is
partially successful — the ordinal family shifts depending on what the ordinal
points at, and the shift is not stable across programs. Consequently the shipped
decoder:

* reports block type, instance, pin names, pin directions and pin data types
  exactly;
* pairs pin *values* only where an input annotation sits on the same row as the
  input pin (left-hand side) or an output annotation on the same row as the output
  pin (right-hand side) — measured at **100 % precision (8/8 correct, 0 wrong) and
  16 % recall** against the PLCopen XML export of the same six programs;
* counts unmatched pins in `GeSheet.unresolved` rather than guessing them.

Precision was chosen over recall deliberately: a silently wrong tag name in
generated code is far worse than a missing one. For complete graphical logic, use
the PLCopen XML export.

---

## 6. Reproducing these findings

```bash
# Section arities across every .GE file
python - <<'PY'
import glob, re
from collections import defaultdict
SEC = re.compile(r"^\[([A-Z]+)\]$")
agg = defaultdict(lambda: defaultdict(int))
for p in glob.glob(r"**/*.GE", recursive=True):
    cur = None
    for line in open(p, encoding="utf-8", errors="replace"):
        line = line.strip()
        if not line: continue
        m = SEC.match(line)
        if m: cur = m.group(1); continue
        if cur is None: continue
        toks = line.split()
        if len(toks) == 1 and toks[0].isdigit(): continue
        agg[cur][len(toks)] += 1
for k in sorted(agg): print(k, dict(sorted(agg[k].items())))
PY
```

To verify the decoder against the XML oracle, compare
`Graphical.render_sheet(parse_ge(body))` against `render_xml_body` for the same
program and diff the pin/value dictionaries.
