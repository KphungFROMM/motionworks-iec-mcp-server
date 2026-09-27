"""Tests for the graphical body decoders (``.GE`` and PLCopen LD/FBD)."""

from __future__ import annotations

from motionworks_iec_mcp_server.parsers import graphical
from motionworks_iec_mcp_server.parsers.graphical import (
    _read_sections,
    build_body,
    parse_ge,
    render_sheet,
)

from .conftest import GE_TWO_BLOCKS


class TestSectionReading:
    def test_count_line_is_dropped(self):
        text = "[LIT]\n2\n1 2 3 4 512 \"\"\n5 6 7 8 512 \"\"\n"
        sections = _read_sections(text)
        assert len(sections["LIT"]) == 2

    def test_repeated_header_accumulates(self):
        """Regression: real files write a second bare ``[CON]`` after the rows.

        Resetting on the repeated header discards half the wiring table.
        """

        text = "[CON]\n1\n1 2 3 4 5 6 7\n[CON]\n"
        sections = _read_sections(text)
        assert len(sections["CON"]) == 1

    def test_multiple_bare_count_lines_are_dropped(self):
        """``[CON]`` in real files carries two bare integers before its rows."""

        text = "[CON]\n1\n7\n1 2 3 4 5 6 7\n"
        sections = _read_sections(text)
        assert len(sections["CON"]) == 1

    def test_bare_int_row_is_not_data(self):
        text = "[VER]\n2\n1 2 3 4 5 6 7 8 9 10 11\n3\n"
        sections = _read_sections(text)
        assert len(sections["VER"]) == 1


class TestGeParsing:
    def test_blocks_and_pins_are_decoded(self):
        _header, _decl, body = _split(GE_TWO_BLOCKS)
        sheet = parse_ge(body)
        assert [b.type_name for b in sheet.blocks] == ["MC_Power", "INT_TO_REAL"]
        assert sheet.blocks[0].instance == "MC_ServoOn"

    def test_pin_groups_are_separated_per_block(self):
        """``[FPT]`` is grouped per block; grouping is recovered from vertical gaps."""

        _header, _decl, body = _split(GE_TWO_BLOCKS)
        sheet = parse_ge(body)
        assert [len(b.pins) for b in sheet.blocks] == [3, 4]

    def test_pin_direction_comes_from_field_eight(self):
        """Regression: reading the direction from field 9 inverts every pin.

        ``[FPT]`` is ``x1 y1 x2 y2 Name 0 0 0 <dir> <flags> 0 @ <TYPE>``. Field 8
        is the direction: ``0`` input, ``1`` output, ``2`` inOut. Field 9 is a
        flag word such as 4224 or 128.

        Reading the direction from field 9 gives every pin ``4224``/``0``-style
        values, so every pin comes back classified as an input.
        """

        _header, _decl, body = _split(GE_TWO_BLOCKS)
        sheet = parse_ge(body)
        mc_power = sheet.blocks[0]

        assert [p.name for p in mc_power.inputs] == ["Enable", "Execute"]
        # Axis is declared direction 2 (inOut). The model has only input/output,
        # so an inOut pin is exposed as an output — never as an input.
        assert [p.name for p in mc_power.outputs] == ["Axis"]
        assert all(p.direction != "4224" for p in mc_power.pins)

    def test_pin_types_are_read(self):
        _header, _decl, body = _split(GE_TWO_BLOCKS)
        sheet = parse_ge(body)
        by_name = {p.name: p for p in sheet.blocks[0].pins}
        assert by_name["Axis"].datatype == "AXIS_REF"
        assert by_name["Enable"].datatype == "BOOL"

    def test_output_pins_are_classified_as_outputs(self):
        _header, _decl, body = _split(GE_TWO_BLOCKS)
        sheet = parse_ge(body)
        assert "IN1" in [p.name for p in sheet.blocks[1].inputs]
        assert "ENO" in [p.name for p in sheet.blocks[1].outputs]

    def test_fbs_width_height_are_not_a_corner(self):
        """``[FBS]`` fields 4-5 are width/height and are often ``0 0``.

        Treating them as a rectangle makes every block zero-sized and detaches
        every pin, so the pin extent has to be adopted instead.
        """

        _header, _decl, body = _split(GE_TWO_BLOCKS)
        sheet = parse_ge(body)
        assert sheet.blocks[0].width == 0 and sheet.blocks[0].height == 0
        assert len(sheet.blocks[0].pins) == 3
        block = sheet.blocks[0]
        for pin in block.pins:
            assert block.x1 <= pin.x2 <= block.x2
            assert block.y1 <= pin.y1 <= block.y2

    def test_contacts_are_decoded(self):
        _header, _decl, body = _split(GE_TWO_BLOCKS)
        sheet = parse_ge(body)
        assert [c.text for c in sheet.contacts] == ["EnableRK"]

    def test_annotations_are_text_elements(self):
        _header, _decl, body = _split(GE_TWO_BLOCKS)
        sheet = parse_ge(body)
        assert "TopCutter" in [t.text for t in sheet.texts]

    def test_canvas_is_read(self):
        _header, _decl, body = _split(GE_TWO_BLOCKS)
        sheet = parse_ge(body)
        assert sheet.canvas[0] == 800

    def test_sections_seen_lists_every_section(self):
        _header, _decl, body = _split(GE_TWO_BLOCKS)
        sheet = parse_ge(body)
        for name in ("GRA", "LIT", "TET", "FBS", "FPT", "KOT", "VER", "CON"):
            assert name in sheet.sections_seen


class TestGeRendering:
    def test_nets_are_produced_per_block(self):
        _header, _decl, body = _split(GE_TWO_BLOCKS)
        nets = render_sheet(parse_ge(body))
        blocks = [n for net in nets for n in net.nodes]
        assert sorted(b.type_name for b in blocks) == ["INT_TO_REAL", "MC_Power"]

    def test_rendered_text_names_block_and_params(self):
        _header, _decl, body = _split(GE_TWO_BLOCKS)
        result = build_body(body, "LD")
        assert "MC_Power(MC_ServoOn)" in result.code
        assert "Axis=TopCutter" in result.code

    def test_instance_label_is_not_used_as_a_pin_value(self):
        """The block's own name is drawn as an annotation and must not become one."""

        _header, _decl, body = _split(GE_TWO_BLOCKS)
        result = build_body(body, "LD")
        assert "Enable=MC_ServoOn" not in result.code

    def test_contacts_become_nets(self):
        _header, _decl, body = _split(GE_TWO_BLOCKS)
        result = build_body(body, "LD")
        assert "-> EnableRK" in result.code

    def test_reported_pin_values_are_never_empty(self):
        _header, _decl, body = _split(GE_TWO_BLOCKS)
        result = build_body(body, "LD")
        for net in result.nets:
            for node in net.nodes:
                for pin, value in node.params.items():
                    assert value, f"{node.type_name}.{pin} has an empty value"

    def test_empty_worksheet_is_safe(self):
        result = build_body("[GRA]\n100 100 0 0 0 0 0\n", "LD")
        assert result.nets == []


def _split(text: str):
    from motionworks_iec_mcp_server.parsers import st as stp

    return stp.split_pou_file(text)
