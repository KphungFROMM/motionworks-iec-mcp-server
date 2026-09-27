"""Shared fixtures and synthetic samples for the MotionWorks IEC test suite.

Unit tests run entirely on synthetic snippets committed here, so they work
anywhere. Integration tests additionally run against real exports when the
``MOTIONWORKS_SAMPLES`` environment variable points at a folder containing the
``PLCOpen XML Export``, ``Extended IEC 61131-2 Export`` and
``Motion Works IEC Program`` directories.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

# --------------------------------------------------------------------------
# Synthetic Extended IEC 61131-2 export
# --------------------------------------------------------------------------

GVB = """\r
(*Group:System Variables*)


VAR_GLOBAL
\tPLC_SYS_TICK_CNT\tAT %MD1.0 :\tDINT;
\tPLCMODE_RUN\tAT %MX1.7.0 :\tBOOL;(**)
\tPLCMODE_STOP\tAT %MX1.6.0 :\tBOOL;(**)
END_VAR


(*Group:AXIS1 <SGD7S> - Sigma-7S Servo Amplifier*)


VAR_GLOBAL
\tAXIS1_SI1_POT\tAT %IX53376.0 :\tBOOL;(*POT, default on pin #7*)
\tAXIS1_BRK\tAT %QX53376.1 :\tBOOL;(*Brake*)
END_VAR
"""

STARTER_ST = """\
(*@PROPERTIES_EX@
TYPE: POU
LOCALE: 0
IEC_LANGUAGE: ST
PLC_TYPE: independent
PROC_TYPE: independent
*)
(*@KEY@:DESCRIPTION*)
Startup logic for the demo axis.
(*@KEY@:END_DESCRIPTION*)
PROGRAM Starter

(*Group:Default*)


VAR_EXTERNAL
\tAxis1 :\tAXIS_REF;(*External Encoder - 21 (Do Not Modify!!) *)
\tTopCutter_Homed :\tBOOL;(*Top Cutter Axis is Homed*)
END_VAR


VAR
\txPermit :\tBOOL := FALSE;
\tiState :\tINT := 0;
END_VAR


(*@KEY@: WORKSHEET
NAME: Starter
IEC_LANGUAGE: ST
*)
Axis1.AxisNum := UINT#1;
IF TopCutter_Homed AND xPermit THEN
\tiState := iState + 1;   (* step *)
END_IF;
(*@KEY@: END_WORKSHEET *)
END_PROGRAM
"""

# A .GE worksheet. Deliberately includes the three traps seen in real files: the
# section header is repeated, [CON] carries two bare count lines, and [FBS]
# width/height are `0 0`.
#
# The two blocks are vertically separated the way real exports separate them,
# because the pin-grouping heuristic splits on the vertical gap between blocks.
# This matches real TopCutter/ServoTaskSlow.GE, which has the same shape.
GE_TWO_BLOCKS = """\
(*@PROPERTIES_EX@
TYPE: POU
LOCALE: 0
IEC_LANGUAGE: LD
PLC_TYPE: independent
PROC_TYPE: independent
*)
(*@KEY@:DESCRIPTION*)
Two-block wiring sample for tests.
(*@KEY@:END_DESCRIPTION*)
PROGRAM Wiring

(*Group:Default*)


VAR_EXTERNAL
\tTopCutter :\tAXIS_REF;
\tSVON_Cmd :\tBOOL;
\tFaultReset :\tBOOL;
\tFlag_In :\tBOOL;
\tScaled :\tINT;
END_VAR


(*@KEY@: WORKSHEET
NAME: Wiring
IEC_LANGUAGE: LD
*)
[GRA]
800 400 0 0 0 0 0

[LIT]
0

[TET]
6
35 9 58 11 4 5 TopCutter
110 9 120 11 4 3 TopCutter
35 13 58 15 4 5 SVON_Cmd
35 17 58 19 4 5 FaultReset
60 90 90 92 4 5 Flag_In
110 90 130 92 4 3 Scaled

[FBS]
2
983075 458758 58 9 0 0 MC_Power MC_ServoOn
262269 327758 58 90 1 0 INT_TO_REAL INT_TO_REAL_1

[FPT]
7
35 9 58 11 Axis 0 0 0 2 128 128 @ AXIS_REF
35 13 58 15 Enable 0 0 0 0 4224 0 @ BOOL
22 17 58 19 Execute 0 0 0 0 4224 0 @ BOOL
35 90 58 92 IN1 0 0 0 0 4224 0 @ BOOL
80 95 98 97 IN2 0 0 0 0 4224 0 @ BOOL
78 93 98 95 ENO 0 0 0 1 0 4224 @ BOOL
76 91 98 93 ENO 0 0 0 1 0 4224 @ BOOL
[AKT]
0

[STT]
0

[TRT]
0

[KOT]
1
70 110 74 112 0 0 6 EnableRK

[VER]
0

[IBX]
0

[CON]
1
7
1 1 1 2 35 9 58 11
0 1 2 35 9 58 11
0 1 3 35 13 58 15
2 0 1 40 17 58 19
0 0 60 90 90 92
1 4 0 80 90 98 92
[CON]

(*@KEY@: END_WORKSHEET *)
END_PROGRAM
"""

TYPES_IEC = """\
(*@PROPERTIES_EX@
TYPE: DATA_TYPE
LOCALE: 0
*)

TYPE
\tCamStruct : STRUCT
\t\tPartLength\t\t: LREAL;\t(*  Part length in mm  *)
\t\tKnifeDiameter\t: LREAL;\t(*  Knife diameter  *)
\t\tCount\t\t\t: UDINT;
\tEND_STRUCT;
END_TYPE
"""

TASK_EXP = """\
(*@KEY@: TASK
\tNAME: 'FastTsk'
\tTYPE: USER_TASK
\tPLCTYPE: eCLR
\tPROCTYPE: MP3300iec
\tTASKTYPE: CYCLIC
*)
\t(*@KEY@: PGINSTANCE
\t\tNAME: 'Starter'
\t\tPLCTYPE: eCLR
\t\tPROCTYPE: MP3300iec
\t\tPARANAME: Starter
\t*)
\t(*@KEY@: END_PGINSTANCE*)

\t(*@KEY@: FILE 'FastTsk.SET'*)
TASK FastTsk
(TYPE := CYCLIC,
INTERVAL := T#4ms,
PRIORITY := 0,
WATCHDOG := 4
);
\t(*@KEY@: END_FILE*)

(*@KEY@: END_TASK*)
"""

RESOURCE_EXP = """\
(*@KEY@: RESOURCE
\tNAME: 'Resource'
\tTYPE: USER_RESOURCE
\tPLCTYPE: eCLR
\tPROCTYPE: MP3300iec
*)
\t(*@KEY@: TASKS NAME: 'Tasks'*)
\t\t(*@KEY@: TASK NAME: 'FastTsk'*)
\t(*@KEY@: END_TASKS*)
\t(*@KEY@: INCLUDE_GLOVAR: 'Global_Variables.GVB'*)
\t(*@KEY@: INCLUDE_IOC: 'IOCONFIGURATION.EIO'*)
(*@KEY@: END_RESOURCE*)
"""

IO_EIO = """\
(*YEA Input Group <Controller I/O>*)
PROGRAM IMO1 WITH FastTsk : INPUT
(
\tVAR_ADR := 61440,
\tEND_VAR_ADR := 61440,
\tDEVICE := DRIVER,
\tDRIVER_NAME := 'LIODrv',
\tDRIVER_PAR1 := 1,
\tDRIVER_PAR2 := 0,
\tDRIVER_PAR3 := 0,
\tDRIVER_PAR4 := 0,
\tDATA_TYPE := BYTE
);
(*YEA Input Group <SGD7S> Network #1 Node #1*)
PROGRAM IAX1 WITH FastTsk : INPUT
(
\tVAR_ADR := 53248,
\tEND_VAR_ADR := 53259,
\tDEVICE := DRIVER,
\tDRIVER_NAME := 'SNIODrv',
\tDRIVER_PAR1 := 1,
\tDRIVER_PAR2 := 0,
\tDRIVER_PAR3 := 1,
\tDRIVER_PAR4 := 0,
\tDATA_TYPE := WORD
);
"""

# --------------------------------------------------------------------------
# Synthetic native project
# --------------------------------------------------------------------------

NODES_LST = """\
CONFIGURATION\t2\tConfiguration\t\teCLR
RESOURCE\t3\tResource\t\t\tMP3300iec
TASKDIR\t4\tTasks\t\teCLR\tMP3300iec
TASK\t5\tFastTsk\t\teCLR\tMP3300iec
PROGRAM\t6\tStarter\tStarter\teCLR\tMP3300iec
PROGRAM\t6\tFiller\tFiller\teCLR\tMP3300iec
VAR_GLOBALS\t4\tGlobal_Variables\tC\\Resource\\Global_Variables.VGR\t\t
IO/CONFIGURATION\t4\tIO_Configuration\tC\\Resource\\IOCONFIG.CNF\t\t
"""

POU_DEPENDENCIES = """\
PouDep.Cache;schema 1
0,Helper,3,FB
1,Starter,2,PG; 0,Helper
2,Filler,2,PG
3,FastTsk,1,TA; 1,Starter; 2,Filler
"""

OCIRES_INI = """\
[eCLR]
ProjectSignature=-1399392433277513436
[Parameters]
ResourceProcType=eCLR
ProcessorType=MP3300iec
"""

FASTTSK_SET = """\
TASK FastTsk
(TYPE := CYCLIC,
INTERVAL := T#4ms,
PRIORITY := 0,
WATCHDOG := 4
WATCHDOG_DISPLAY := 4
WATCHDOG_ENABLED := YES
);
"""

# --------------------------------------------------------------------------
# Synthetic PLCopen XML
# --------------------------------------------------------------------------

PLCOPEN_XML = """\
<?xml version="1.0" encoding="utf-8"?>
<project xmlns="http://www.plcopen.org/xml/tc6.xsd">
  <fileHeader companyName="Yaskawa" productName="MotionWorks IEC 3 Pro"
              productVersion="3.7.5.1" creationDateTime="2024-01-01T00:00:00"/>
  <contentHeader name="SampleProject" version="1">
    <coordinateInfo><pageSize x="999" y="3600"/></coordinateInfo>
  </contentHeader>
  <types>
    <dataTypes>
      <dataType name="AXIS_REF">
        <baseType><struct>
          <variable name="AxisNum"><type><UINT/></type></variable>
        </struct></baseType>
      </dataType>
      <dataType name="CamStruct">
        <baseType><struct>
          <variable name="PartLength"><type><LREAL/></type></variable>
          <variable name="KnifeDiameter"><type><LREAL/></type></variable>
        </struct></baseType>
      </dataType>
      <dataType name="ModeEnum">
        <baseType><enum>
          <value name="Off" value="0"/>
          <value name="On" value="1"/>
        </enum></baseType>
      </dataType>
    </dataTypes>
    <pous>
      <pou name="Starter" pouType="program">
        <interface>
          <externalVars>
            <variable name="TopCutter_Homed" address="%MX1.7.0"
                      comment="Top Cutter Axis is Homed"><type><BOOL/></type></variable>
          </externalVars>
          <localVars>
            <variable name="xPermit"><type><BOOL/></type>
              <initialValue><simpleValue value="FALSE"/></initialValue>
            </variable>
          </localVars>
        </interface>
        <body>
          <ST>
            <html xmlns="http://www.w3.org/1999/xhtml">
              <p xml:space="preserve" xmlns="http://www.w3.org/1999/xhtml">
                <br/>xPermit := TRUE;<br/>
                <br/>IF TopCutter_Homed THEN<br/>&#9;xPermit := FALSE;<br/>END_IF;<br/>
              </p>
            </html>
          </ST>
        </body>
      </pou>
      <pou name="Wiring" pouType="program">
        <interface/>
        <body>
          <LD>
            <leftPowerRail localId="1"><position x="0" y="0"/></leftPowerRail>
            <contact localId="2" negated="false">
              <position x="10" y="10"/>
              <variable>Enable_Cmd</variable>
              <connection refLocalId="4"/>
            </contact>
            <block localId="4" typeName="MC_Power" instanceName="MC_1" width="20" height="20">
              <position x="30" y="10"/>
              <inputVariables>
                <variable formalParameter="Enable">
                  <connection refLocalId="2"/>
                </variable>
              </inputVariables>
              <inOutVariables>
                <variable formalParameter="Axis">
                  <connection refLocalId="5"/>
                </variable>
              </inOutVariables>
              <outputVariables>
                <variable formalParameter="Status">
                  <connection refLocalId="6"/>
                </variable>
              </outputVariables>
            </block>
            <inVariable localId="5" width="10" height="2">
              <position x="20" y="20"/>
              <variable>TopCutter</variable>
            </inVariable>
            <coil localId="6" storage="set">
              <position x="60" y="10"/>
              <variable>Servo_On</variable>
            </coil>
          </LD>
        </body>
      </pou>
      <pou name="Empty" pouType="program">
        <interface/>
        <body><ST><html xmlns="http://www.w3.org/1999/xhtml"><p/></html></ST></body>
      </pou>
    </pous>
  </types>
  <instances>
    <configurations>
      <configuration name="Configuration">
        <resource name="Resource">
          <task priority="0" interval="00:00:00.4">
            <programInstance name="Starter" type="Starter"/>
          </task>
          <task priority="0" single="@spg@WarmStart">
            <programInstance name="Empty" type="Empty"/>
          </task>
          <globalVars name="Sys">
            <variable name="PLCMODE_RUN" address="%MX1.7.0"
                      comment="TRUE if running"><type><BOOL/></type></variable>
          </globalVars>
        </resource>
      </configuration>
    </configurations>
  </instances>
</project>
"""


# --------------------------------------------------------------------------
# Firmware library help pages
# --------------------------------------------------------------------------
#
# These mirror the structure of the pages MotionWorks installs, decompiled from
# `FW_LIB\*\*.chm` with `hh.exe -decompile`. The shapes that matter and are
# reproduced exactly:
#
#   * a banner table that re-draws the page title in an <h1>/<h2>, which is why
#     the subject must be read from <title> rather than the first <h2>;
#   * a <table class="intro"> between the banner and the prose description;
#   * scope banners that span one cell (`VAR_IN_OUT`) or two (`VAR_INPUT` plus the
#     `Default` column label);
#   * `class="unsupported"` on the <tr> of a pin this firmware does not implement;
#   * data types declared as `*`, Element, Data Type, Description, Usage;
#   * enumerated types where the numeric value is the row's first cell.

_FB_BANNER = """\
<h1>
<table>
<tbody>
<tr><td>
<h1><span class="SystemTitle">{name}</span></h1>
<p>PLCopen Help Documentation</p>
<p>Help version created <span class="SystemShortDate">12/1/2022</span></p>
</td></tr>
<tr><td><h2><span class="SystemTitle">{name}</span></h2></td></tr>
</tbody>
</table>
</h1>
"""

_FB_PAGE = """\
<?xml version="1.0" encoding="Windows-1252"?>
<html xmlns:MadCap="http://www.madcapsoftware.com/Schemas/MadCap.xsd">
<head><title>MC_Power</title></head>
<body>
<div id="content">
{banner}
<table class="intro"><tr><td><img class="FB" src="../../img/MC/2_MC_Power.jpg" /></td></tr></table>
<p>This Function Block enables or disables the axis.</p>
<h2>Library</h2>
<p>PLCopen_Plus_v2_2a Firmware Library</p>
<h2>Parameters</h2>
<table class="fb_parameters">
<col /><col /><col /><col /><col />
<tr>
<th><p><a class="tooltip" title="PLCopen requirement level">*</a></p></th>
<th><p>Parameter</p></th>
<th><p>Data type</p></th>
<th colspan="2"><p>Description</p></th>
</tr>
<tr><td class="group" colspan="5"><p>VAR_IN_OUT</p></td></tr>
<tr>
<td><p>B</p></td>
<td><p>Axis</p></td>
<td><p><a href="../../Common_Datatypes/Data_Type__AXIS_REF.htm" id="a3">AXIS_REF</a></p></td>
<td colspan="2"><p>Logical axis reference. This value can be located on the Configuration tab.</p></td>
</tr>
<tr>
<td class="group" colspan="4"><p>VAR_INPUT</p></td>
<td class="group"><p>Default</p></td>
</tr>
<tr>
<td><p>B</p></td>
<td><p>Enable</p></td>
<td><p>BOOL</p></td>
<td><p>The function will continue to execute every scan while Enable is held high.</p></td>
<td><p>FALSE</p></td>
</tr>
<tr class="unsupported">
<td><p>E</p></td>
<td><p>Enable_Positive</p></td>
<td><p>BOOL</p></td>
<td><p>Not supported; reserved for future use.</p></td>
<td><p>FALSE</p></td>
</tr>
<tr class="unsupported">
<td><p>E</p></td>
<td><p>BufferMode</p></td>
<td><p><a href="../Overview/FBInterface.htm#FBInterface_BufferMode">MC_BufferMode</a></p></td>
<td><p>Not supported; reserved for future use.</p></td>
<td><p>MC_BufferMode#Aborting</p></td>
</tr>
<tr><td class="group" colspan="4"><p>VAR_OUTPUT</p></td><td class="group"><p>Default</p></td></tr>
<tr>
<td><p>B</p></td>
<td><p>Status</p></td>
<td><p>BOOL</p></td>
<td><p>Actual state of the axis, TRUE=Enabled.</p></td>
<td><p>&#160;</p></td>
</tr>
<tr>
<td><p>B</p></td>
<td><p>ErrorID</p></td>
<td><p><a href="Function_Block_ErrorID_List.htm" id="a9">UINT</a></p></td>
<td><p>If Error is true, this output provides the ErrorID.</p></td>
<td><p>&#160;</p></td>
</tr>
</table>
<h2>Notes</h2>
<p>Calling MC_Power with Enable true while the axis is Disabled may fail.</p>
<h2>Related Function Blocks</h2>
<p>MC_Reset, MC_ReadAxisError</p>
<h2>Error Description</h2>
<p>See the ErrorID list.</p>
<h2>Example</h2>
<p>Initializing an axis and using MC_Power to enable the axis.</p>
</div>
</body>
</html>
"""

REAL_FB_PAGE = _FB_PAGE.format(banner=_FB_BANNER.format(name="MC_Power")).encode("cp1252")

REAL_DATATYPE_PAGE = """\
<?xml version="1.0" encoding="Windows-1252"?>
<html xmlns:MadCap="http://www.madcapsoftware.com/Schemas/MadCap.xsd">
<head><title>AXIS_REF</title></head>
<body>
<div id="content">
{banner}
<p>Logical axis reference.</p>
<h2>Data Type Declaration</h2>
<table class="fb_datatypes">
<tr><td><p>&#160;</p></td><td><p>*</p></td><td><p>Element</p></td>
    <td><p>Data Type</p></td><td><p>Description</p></td><td><p>Usage</p></td></tr>
<tr><td><p>&#160;</p></td><td><p>&#160;</p></td><td><p>MyAxisRef</p></td>
    <td><p>AXIS_REF</p></td><td><p>&#160;</p></td><td><p>&#160;</p></td></tr>
<tr><td><p>U</p></td><td><p>AxisNum</p></td><td><p>UINT</p></td>
    <td><p>The value of AxisNum is determined by the logical axis number assigned by the Hardware Configuration.</p></td>
    <td><p>MyAxisRef.AxisNum</p></td></tr>
</table>
<h2>Example</h2>
<p>MyServo.AxisNum:=UINT#3;</p>
</div>
</body>
</html>
""".format(banner=_FB_BANNER.format(name="DataType: AXIS_REF")).encode("cp1252")

REAL_ENUM_INDEX_PAGE = """\
<?xml version="1.0" encoding="Windows-1252"?>
<html xmlns:MadCap="http://www.madcapsoftware.com/Schemas/MadCap.xsd">
<head><title>Enumerated_Types</title></head>
<body>
<div id="content">
{banner}
<p>Enumerated types used by the function blocks.</p>
<h2>Enumerated Types</h2>
<table class="fb_datatypes">
<tr><td><p>MC_BufferMode</p></td><td><p>&#160;</p></td></tr>
<tr><td><p>0</p></td><td><p>Aborting</p></td>
    <td><p>This is the Default mode. The FB aborts an ongoing motion immediately.</p></td></tr>
<tr><td><p>1</p></td><td><p>Buffered</p></td>
    <td><p>The FB affects the axis as soon as the previous movement is complete.</p></td></tr>
<tr><td><p>2</p></td><td><p>BlendingLow</p></td><td><p>Blended with the lowest velocity.</p></td></tr>
<tr><td><p>&#160;</p></td><td><p>&#160;</p></td></tr>
<tr><td><p>MC_Direction</p></td><td><p>&#160;</p></td></tr>
<tr><td><p>0</p></td><td><p>Positive_Direction</p></td>
    <td><p>In a rotary application, forces the axis to move in a positive direction.</p></td></tr>
<tr><td><p>1</p></td><td><p>Shortest_Way</p></td>
    <td><p>For use where the Load Type is configured as a rotary or modularized axis.</p></td></tr>
<tr><td><p>2</p></td><td><p>Negative_Direction</p></td>
    <td><p>In a rotary application, forces the axis to move in a negative direction.</p></td></tr>
<tr><td><p>&#160;</p></td><td><p>&#160;</p></td></tr>
</table>
</div>
</body>
</html>
""".format(banner=_FB_BANNER.format(name="Enumerated_Types")).encode("cp1252")

# An enum that is described on its own page as well as in the index. `Both` exists
# only here, which is what makes the merge observable.
REAL_ENUM_TYPE_PAGE = """\
<?xml version="1.0" encoding="Windows-1252"?>
<html xmlns:MadCap="http://www.madcapsoftware.com/Schemas/MadCap.xsd">
<head><title>MC_Direction</title></head>
<body>
<div id="content">
{banner}
<p>Direction of motion for positioning function blocks.</p>
<h2>Data Type Declaration</h2>
<table class="fb_datatypes">
<tr><td><p>Enumerated Type</p></td><td><p>#INT Value</p></td>
    <td><p>Enum Value</p></td><td><p>Description</p></td></tr>
<tr><td><p>MC_Direction</p></td><td><p>Direction of motion.</p></td></tr>
<tr><td><p>0</p></td><td><p>Positive_Direction</p></td>
    <td><p>Forces the axis to move in a positive direction.</p></td></tr>
<tr><td><p>9</p></td><td><p>Both</p></td><td><p>Direction is not restricted.</p></td></tr>
</table>
</div>
</body>
</html>
""".format(banner=_FB_BANNER.format(name="DataType: MC_Direction")).encode("cp1252")



@pytest.fixture
def extended_export(tmp_path: Path) -> Path:
    """A synthetic Extended IEC 61131-2 export directory."""

    root = tmp_path / "SampleExtended"
    root.mkdir()
    (root / "Starter.ST").write_text(STARTER_ST, encoding="utf-8")
    (root / "Wiring.GE").write_text(GE_TWO_BLOCKS, encoding="utf-8")
    (root / "CamTypes.IEC").write_text(TYPES_IEC, encoding="utf-8")
    (root / "PHYSHARDWARE.EXP").write_text(
        "(*@KEY@: PHYSICAL_HARDWARE\n\tNAME: 'Physical Hardware'\n*)\n", encoding="utf-8"
    )

    cfg = root / "Configuration" / "Resource"
    cfg.mkdir(parents=True)
    (cfg / "Global_Variables.GVB").write_text(GVB, encoding="utf-8")
    (cfg / "IOCONFIGURATION.EIO").write_text(IO_EIO, encoding="utf-8")
    (cfg / "FastTsk.EXP").write_text(TASK_EXP, encoding="utf-8")
    (cfg / "RESOURCE.EXP").write_text(RESOURCE_EXP, encoding="utf-8")
    return root


@pytest.fixture
def native_project(tmp_path: Path) -> Path:
    """A synthetic native MotionWorks IEC project directory."""

    root = tmp_path / "SampleNative"
    (root / "C" / "Resource" / "R" / "Resource").mkdir(parents=True)
    (root / "POE" / "Starter").mkdir(parents=True)
    (root / "NODES.LST").write_text(NODES_LST, encoding="utf-8")
    (root / "PROJECT.INF").write_text(
        "[@_ProjInfo_@]\nLastChange=01/01/2026  12:00:00 PM\n", encoding="utf-8"
    )
    res = root / "C" / "Resource" / "R" / "Resource"
    (res / "eCLRPouDependencies.dat").write_text(POU_DEPENDENCIES, encoding="utf-8")
    (res / "OCIRES.INI").write_text(OCIRES_INI, encoding="utf-8")
    (res / "FastTsk.SET").write_text(FASTTSK_SET, encoding="utf-8")
    # A binary stub: the real file is a compound binary and must never be read as text.
    (root / "POE" / "Starter" / "src.st1").write_bytes(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1\x00\x00")
    return root


@pytest.fixture
def plcopen_file(tmp_path: Path) -> Path:
    """A synthetic PLCopen XML export file."""

    path = tmp_path / "SampleProject.xml"
    path.write_text(PLCOPEN_XML, encoding="utf-8")
    return path


@pytest.fixture
def samples() -> Path | None:
    """Real exports, when ``MOTIONWORKS_SAMPLES`` points at them."""

    raw = os.environ.get("MOTIONWORKS_SAMPLES", "").strip()
    if not raw:
        return None
    path = Path(raw)
    if not path.is_dir():
        return None
    return path


@pytest.fixture(autouse=True)
def _clear_parse_cache():
    """Keep module-level caching from leaking between tests."""

    from motionworks_iec_mcp_server.project import clear_cache

    clear_cache()
    yield
    clear_cache()
