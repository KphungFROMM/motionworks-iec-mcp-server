# Workflow — from a MotionWorks project to reviewed code

This is the practical guide: what to install, what to export from the IDE, what to
point the agent at, and how generated code gets back into the project.

The export and import steps below were read out of MotionWorks' own installed help
(`Help\XMLImportExport*.chm`) rather than guessed, and are quoted where it matters.
Anything I could not verify is marked as such instead of being smoothed over.

---

## 1. Install (once)

Not published on PyPI, so install from the repository:

```bash
git clone https://github.com/KphungFROMM/motionworks-iec-mcp-server.git
cd motionworks-iec-mcp-server
python -m venv .venv
.venv/Scripts/python -m pip install -e ".[dev]"
```

`.[dev]` adds pytest so you can run the suite; `pip install -e .` alone is enough to
run the server. On macOS or Linux the path is `.venv/bin/python`.

Point your MCP client at the console script — an absolute path, because the client
starts it from its own working directory:

```json
{
  "mcpServers": {
    "motionworks": {
      "command": "C:/path/to/motionworks-iec-mcp-server/.venv/Scripts/motionworks-iec-mcp-server.exe",
      "args": []
    }
  }
}
```

On macOS/Linux use `.venv/bin/motionworks-iec-mcp-server`. Everything except the
firmware library reference works there; see §7.

### If your client is an agent harness, use the launcher instead

Point the client at `scripts\motionworks-iec-mcp-server.cmd` and leave Arguments and
Environment empty:

```json
{
  "mcpServers": {
    "motionworks": {
      "command": "C:/path/to/motionworks-iec-mcp-server/scripts/motionworks-iec-mcp-server.cmd",
      "args": []
    }
  }
}
```

It does the same thing as the console script, but clears `PYTHONHOME`, `PYTHONPATH`
and `PYTHONSTARTUP` first. That is not defensive padding — it fixes a concrete
failure. Some harnesses export `PYTHONHOME` for their own bundled Python, and a venv
`python.exe` that inherits a `PYTHONHOME` pointing somewhere invalid dies *during
interpreter startup*, before any of this project's code runs:

```
Fatal Python error: init_fs_encoding: failed to get the Python codec
ModuleNotFoundError: No module named 'encodings'
```

Measured, both under a hostile `PYTHONHOME`:

| Command | Clean env | Hostile env |
|---|---|---|
| `.venv\Scripts\motionworks-iec-mcp-server.exe` | works, 28 tools | **no response at all** |
| `scripts\motionworks-iec-mcp-server.cmd` | works, 28 tools | works, 28 tools |

The symptom in the client is a server that simply never connects, with nothing in
the log to explain it. `tests/test_launcher.py` pins this behaviour.

If you would rather not use the launcher, set `PYTHONHOME` to an empty value in the
client's Environment field instead. Setting it to empty works; leaving it inherited
from a hostile harness does not.

### Adding it through a GUI that asks for fields

Some harnesses (DSH / AryaAI, and others with a "Add MCP server" dialog) ask for the
command as separate fields. Fill them in like this:

| Field | Value |
|---|---|
| **Transport** | `stdio` |
| **Name** | `motionworks` — a short lowercase id; it becomes the tool prefix, e.g. `motionworks.open_project` |
| **Command** | `C:\path\to\motionworks-iec-mcp-server\scripts\motionworks-iec-mcp-server.cmd` |
| **Arguments** | *leave empty* — stdio takes no arguments |
| **Environment** | *leave empty* — the launcher already clears the Python variables |

Two things worth knowing:

* The **Command must be absolute.** Clients launch it from their own working
  directory, so a relative path resolves somewhere else entirely.
* Prefer the `.cmd` launcher over the `.exe` in these dialogs specifically, because
  you cannot see what environment the harness hands the process. See above.

If you would rather point straight at the `.exe`, add this to Environment to be safe:

```
PYTHONHOME=
```

Nothing else needs configuring. The server finds your MotionWorks installation
itself, and the first call that needs library knowledge extracts the vendor help
into `%LOCALAPPDATA%\motionworks-iec-mcp\fwlib` (about 1.4 s, once per version).
Check it with `get_library_reference_status`.

---

## 2. Export the project

### PLCopen XML export — the primary artifact

From the vendor help, **File → Export**:

1. Select the node in the project tree. **`Project` exports everything** — all POUs
   (FBD, LD, IL, ST and SFC), data types, and the entire `Physical Hardware` subtree.
   Selecting a narrower node (`POU`, `Tasks`, `Global variables`, …) exports just
   that subtree, which is fine if that is all you need.
2. `File` → `Export`. The **Import / Export** dialog appears.
3. Mark **"Export PLCopen xml file"** and confirm.
4. In **Export as XML file**, choose the folder and file name.
5. Leave **Save as type** on **V1.01**. The help is explicit that V0.99 should not
   be used: *"PLCopen has modified its XML schema specification. Please note that
   V0.99 has never been released officially. Thus, we strongly recommend to use
   always the file type according to V1.01."*

Result: one `.xml` holding every POU, all body languages, the data-type library,
tasks and global variables.

### Extended IEC 61131-2 export — the complement

Do this too. It is the **only** source of some things, and the vendor help says so
in as many words:

> "The XML export/import mechanism does not handle programming system specific
> elements (i.e. elements which are not defined by the PLCopen XML specification,
> such as **'I/O configuration'**)."

So the XML export cannot contain I/O configuration. The Extended IEC 61131-2 export
is what carries it, along with task *names*, `AT %` addresses, and Structured Text
as plain files with original tab alignment.

*Honest caveat:* I could not find this export documented anywhere in the 259 help
files installed with 3.7.5.1 — I decompiled and searched several. The dialog wording
is therefore unverified; the **artifact is not**, and it is unmistakable.

### What a good export looks like

```
PLCOpen XML Export/
    TopCutter.xml                     ← one file per project, all languages

Extended IEC 61131-2 Export/
    TopCutter/                        ← a folder tree per project
        TopCutterCutControl.ST            plain-text ST
        ServoHoming.GE                    ladder / FBD worksheet
        CamTypes.IEC                      data types
        Configuration/Resource/
            Global_Variables.GVB          variables with AT % addresses
            IOCONFIGURATION.EIO           I/O configuration
            FastTsk.EXP                   task name, interval, priority

Motion Works IEC Program/             ← optional, the native project folder
    TopCutter/                            adds the POU/task index and .DIT
                                          function-block interfaces
```

### Re-export whenever the project changes

Parses are cached on file modification time, so a stale export means quietly stale
answers. Exporting again is enough — there is no cache to clear.

---

## 3. Point the agent at the export

`open_project` accepts a file or a directory and detects the source itself. It does
not matter which of the three you hand it.

| You pass | You get |
|---|---|
| `.../PLCOpen XML Export/TopCutter.xml` | that project, all languages, tasks, data types |
| `.../Extended IEC 61131-2 Export/TopCutter` | that project plus I/O config and task names |
| a folder containing both | **merged**, with disagreements reported |

**Point at one project.** A directory is scanned for every export it can find, so
pointing at a folder of *several* projects merges all of them and reports
cross-project divergences, which is noise. If you want one machine, point at that
machine's artifacts.

### If you point at the native project by mistake

The `.mwt` and its folders are the most natural thing to point at, and they are the
one thing that cannot answer questions: the code is in a compound binary. Rather
than returning an empty result, `open_project` and `get_sources` add an
`exportGuidance` block that says what is missing, what it costs, and the export steps
above — so the agent can tell you instead of guessing.

```
issue     : no_export_found        severity: blocking
summary   : Only the native MotionWorks project was found. Its code is stored in a
            proprietary compound binary (src.st1), so nothing here can supply
            source code, variables, types or I/O configuration.
cannot    : any POU source code / variables, types, enums / I/O configuration
can       : POU names, task names, program-to-task assignment, .DIT interfaces
howToExport: plcopenXml [...steps...], extendedIec [...steps...]
foundNearby: [plcopen_xml] TopCutter.xml          ← name matches this project
             [extended_iec] Extended IEC 61131-2 Export/TopCutter
suggestedPaths: ...\PLCOpen XML Export\TopCutter.xml
                ...\Extended IEC 61131-2 Export\TopCutter
nextStep  : Both exports for this project already exist: ... — one open_project call
            reads one path, so copy both into one folder and point at that folder.
```

So if the exports already exist anywhere nearby, you get told where they are rather
than being sent back to the IDE. Two levels up and down from the path you gave are
searched, and a *name match* is preferred — suggesting another machine's export would
be worse than suggesting nothing.

A partial load gets the same treatment, with the reason attached:

| Situation | `issue` | What it tells you |
|---|---|---|
| native project only | `no_export_found` | blocking — no code is readable at all |
| Extended export only | `plcopen_xml_missing` | no type definitions, so no block interfaces; graphical logic only 16 % recovered |
| PLCopen XML only | `extended_iec_missing` | I/O configuration, task names and `AT %` addresses are absent |

### Read the `divergences` list on the first call.** It is often the most important
thing about a project, because the exports are incomplete in different ways. Merging
one sample project's XML and Extended exports reports five:

```
[pou_xml_only]            only in the PLCopen XML export:
                          CamGen, Initialize, StraightCut
[pou_extended_only]       only in the Extended IEC export:
                          TopCutterCamSetup, TopCutterCutControl,
                          TopCutterFFCamSetup, TopCutterInitialize
[task_program_conflict]   FastTsk: the PLCopen XML export lists [StraightCut]
                          while the export that names tasks lists
                          [TopCutterCutControl]
```

That last one is worth pausing on: the two exports are **different generations of the
project**, not two views of the same one. The XML names `StraightCut` where the
Extended export names `TopCutterCutControl`. That is precisely why the server will not
guess — it keeps the named export's assignment and tells you they disagree, rather
than inventing a merge. See the note in §7 about a task with an unnamed schedule.

---

## 4. The loop

| You ask | Tool the agent reaches for | What comes back |
|---|---|---|
| "Read this project" | `open_project` | POUs, variables, tasks, **and the divergences** |
| "What does the machine do?" | `get_pou` / `get_tasks` | ST source; `FastTsk T#4ms`, `MedTsk T#20ms`… |
| "What signals exist?" | `get_tags`, `search_logic` | symbols with `AT %` addresses and comments |
| "Where is X used?" | `search_logic` | every POU, network and line |
| "Which cam blocks are there?" | `search_library`, `list_library_blocks` | the firmware library's own documentation |
| "Add an index move" | `get_fb_signature`, `get_code_conventions` | typed pin list; the project's naming style |
| *(generates code)* | `validate_pou` | `ok: true`, or a specific wrong pin or symbol |
| "Give me the file" | `render_pou_source` | an importable POU |

### A kickoff prompt that pays for itself

> Read the project at `<path>`. Tell me what the machine does, how it is scheduled,
> which exports disagree, and what naming conventions the code follows. Don't write
> code yet.

That one exchange gives the agent the symbols, the task structure, the style and the
library surface. Everything after it is a single round trip.

### A task prompt

> Add a POU that index-moves the top cutter axis 250 mm at 100 mm/s. Follow this
> project's conventions, use the right MC_ block, validate it against the project,
> and give me the file to import.

Behind that, the agent does the things that separate working code from plausible
code: it looks up the block's **real** pin list rather than recalling one, and it
checks its own output against the project's symbol table before you see it.

---

## 5. Getting the code back into the project

Two routes. The first always works; the second is the vendor's documented one.

### A. Paste into a new POU — always available

Create the POU in the MotionWorks project tree, paste the generated body, add the
declared variables. This makes no assumptions about import formats, so it is the
route to fall back on.

### B. PLCopen XML import — the documented path

From the vendor help, **File → Import**:

1. Select the node in the project tree that you want to import into.
2. `File` → `Import`. The **Import / Export** dialog appears.
3. Mark **"Import PLCopen xml file"** and confirm.
4. The **Object types** dialog appears; choose what to bring in.

### Where this stands today

`render_pou_source` emits a POU in the shape MotionWorks' *Extended IEC 61131-2
export* uses — the `(*@PROPERTIES_EX@ …)` header, the `PROGRAM` line, the declaration
blocks, the `(*@KEY@: WORKSHEET …)` region and the closing keyword.

**Verified:** that output parses back through this server's own parser, so the shape
is at least self-consistent and matches the export format byte-for-byte in structure.

**Not verified:** that the IDE imports that shape. That export format is undocumented
in the installed help, so it is an assumption, not a fact.

**Consequence:** use route A, or wrap the POU in PLCopen XML yourself for route B. A
PLCopen XML emitter is the obvious next addition, since that is the documented import
path — see the roadmap in [README.md](README.md).

---

## 6. Which exports to make

| Exports | Readable | Missing |
|---|---|---|
| XML only | all POUs, all languages, data types, tasks | I/O configuration, task *names*, `AT %` addresses |
| Extended only | ST as plain text, addresses, I/O config, task names | no data types at all, so no block interfaces; graphical pin values only 16 % recovered |
| **Both** | everything above, merged | — |
| Both + native project folder | also `.DIT` function-block interfaces and the POU/task index | — |

**Prefer producing the PLCopen XML export even if you only care about ladder logic.**
Without it, graphical logic is understood fully in structure but only 16 % of pin
*values* are recovered — 100 % accurate, just partial.

---

## 7. When something looks wrong

| Symptom | Cause | Fix |
|---|---|---|
| the server never connects, nothing in the log | a harness-set `PYTHONHOME` killed the interpreter at startup | point Command at `scripts\motionworks-iec-mcp-server.cmd`, or set `PYTHONHOME=` in Environment |
| `ModuleNotFoundError: No module named 'encodings'` | same cause — Python never found its own standard library | same fix |
| the tool list is empty or shows 0 tools | the process started but exited; check it runs by hand | run the Command with `--help` in a terminal |
| a POU's task reads `task@10@00:00:01.0` | only the PLCopen XML export describes that task, and it does not carry names | nothing to fix — the *schedule* is known (priority 10, 1 s) even though the name is not |
| `task_program_conflict` divergence | the two exports are different generations of the project | read it; decide which export is current, and re-export the other |
| `typeCount: 0`, no block signatures | Extended export only — it carries no type definitions | produce the PLCopen XML export too |
| `referenceAvailable: false` | the firmware library reference is not available | run `get_library_reference_status`; set `MOTIONWORKS_FWLIB`, or point it at a folder of pre-extracted `.htm` |
| Ladder networks show blocks but few `pin=value` pairs | reading `.GE` rather than XML | export PLCopen XML |
| `pouCount` far larger than expected | you pointed at a folder holding several projects | point at one project's artifacts |
| Answers do not change after editing the project | the export is stale | re-export from the IDE |
| `identifier_case` warnings | references differ in case from the declaration | harmless — IEC identifiers are case-insensitive; matching the declaration is tidier |

The reference needs Windows with `hh.exe` **and** PowerShell. Without them everything
else still works and library signatures fall back to what the project itself reveals
(`derivedFrom` says which route was used). `MOTIONWORKS_FWLIB` can point directly at
a folder of already-extracted HTML to bypass the decompiler entirely.

---

## 8. What this does not do

- **No write-back into `.mwt`.** That container is undocumented binary, and a bad
  write could corrupt a live machine project. Generated code is emitted for you to
  import (§5).
- **No live controller connection.** MotionWorks IEC exposes no documented
  programmatic interface, so this is export-based. Re-export to refresh.
- **No version control, no build.** It reads and reasons; your IDE still compiles,
  downloads and runs.

---

## Worked example

```
You:   Read the project at C:/work/PLCOpen XML Export/TopCutter.xml and
       C:/work/Extended IEC 61131-2 Export/TopCutter.

Agent: Two sources merged, 10 POUs, 281 variables, 7 tasks, processor MP2600iec.
       Divergences matter here: the XML export omits TopCutterCutControl,
       TopCutterCamSetup and TopCutterFFCamSetup, and is the only source for
       StraightCut, CamGen and Initialize — but it ships an empty body for two POUs
       the Extended export has real source for, which is what I used. Named tasks
       are FastTsk (T#4ms, priority 0), MedTsk (T#20ms, 3), SlowTsk (T#100ms, 7),
       BG and Start. CamGen and Initialize run in tasks only the XML describes
       (priority 10 / 1 s, priority 0 / WarmStart), so they are named by schedule.

You:   What drives TopCutter_Homed?

Agent: Written in ServoHoming (ladder), read in four places — TopCutterCutControl
       (ST), EIP_ToCLX (ladder, feeding EIP_ToCLX_Axis_Homed) and ServoTaskSlow.
       It is gated on PLCMODE_RUN.

You:   Add an index move for the top cutter axis.

Agent: Looking at get_code_conventions, this project uses x/i/r prefixes and
       MC_<Thing>_<Action> instance names. I'll use MC_MoveRelative — its real
       parameter list is Axis (AXIS_REF), Execute (BOOL), Distance, Velocity,
       Acceleration, Deceleration, Jerk (LREAL), BufferMode (MC_BufferMode),
       then Done/Busy/Active/CommandAborted/Error/ErrorID. Validated against the
       project: 0 errors. IndexMove.ST is ready to import.
```

Every claim in that transcript comes from a tool result, not from the agent's
recollection — which is the entire point.
