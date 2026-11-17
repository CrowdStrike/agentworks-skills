"""Tests for the trace waterfall renderer (common/scripts/waterfall.py)."""

import math

import pytest

import waterfall


def span(span_id, parent=None, name=None, start: str | None = "2026-11-16T10:00:00Z",
         end: str | None = "2026-11-16T10:00:02Z", duration: int | None = 2000, span_type="aw_agent", **extra):
    return {"span_id": span_id, "parent_span_id": parent, "name": name or span_id, "span_type": span_type,
            "start_time": start, "end_time": end, "duration_ms": duration, "trace_id": "abc123",
            "attributes": {}, **extra}


def lines(spans):
    return waterfall.render_waterfall(spans).split("\n")


class TestParseMs:
    def test_zulu_and_offset_are_the_same_instant(self):
        assert waterfall.parse_ms("2026-11-16T10:00:00Z") == waterfall.parse_ms("2026-11-16T03:00:00-07:00")

    def test_one_second_apart(self):
        assert waterfall.parse_ms("2026-11-16T10:00:01Z") - waterfall.parse_ms("2026-11-16T10:00:00Z") == 1000

    @pytest.mark.parametrize("value", [None, "", "not a time", 12345])
    def test_bad_input_is_nan(self, value):
        assert math.isnan(waterfall.parse_ms(value))


class TestRenderWaterfall:
    def test_children_are_indented_under_their_parent_in_start_order(self):
        out = lines([
            span("root", name="agent"),
            span("b", "root", name="second", start="2026-11-16T10:00:01Z", duration=500, span_type="tool_response"),
            span("a", "root", name="first", start="2026-11-16T10:00:00.5Z", duration=250, span_type="llm"),
        ])
        body = [line for line in out if "│" in line]
        assert [line.split()[0] for line in body] == ["agent", "├─", "└─"]
        assert "first" in body[1] and "second" in body[2]

    def test_header_reports_span_count_and_total(self):
        out = lines([span("root"), span("c", "root", duration=500)])
        assert "2 spans" in out[0] and "total 2.0s" in out[0]

    def test_error_spans_are_flagged(self):
        out = lines([span("root"), span("c", "root", status="error")])
        flagged = [line for line in out if "⚠ error" in line]
        assert len(flagged) == 1 and "c" in flagged[0]

    def test_credits_column(self):
        out = lines([span("root", attributes={"cost.raw_credit_cents": 250})])
        assert "2.50" in next(line for line in out if line.startswith("root"))

    def test_missing_cost_shows_a_dash(self):
        assert waterfall._credits({"attributes": {}}) == "—"  # pylint: disable=protected-access
        assert waterfall._credits({"attributes": {"cost.raw_credit_cents": "n/a"}}) == "—"  # pylint: disable=protected-access

    def test_null_attributes_render_a_dash_for_credits(self):
        row = next(line for line in lines([span("root", attributes=None)]) if line.startswith("root"))
        assert "—" in row

    def test_orphan_is_rendered_as_its_own_root(self):
        """A span whose parent isn't in the set must still appear."""
        out = "\n".join(lines([span("root"), span("orphan", "missing-parent", name="lost")]))
        assert "lost" in out

    def test_parent_cycle_is_rendered_once_each_and_terminates(self):
        """Two spans naming each other as parent must not loop or vanish."""
        out = lines([span("a", "b", name="alpha"), span("b", "a", name="beta")])
        text = "\n".join(out)
        assert text.count("alpha") == 1 and text.count("beta") == 1

    def test_self_parent_is_a_root(self):
        assert "solo" in "\n".join(lines([span("s", "s", name="solo")]))

    def test_duplicate_span_ids_render_both_rows(self):
        out = "\n".join(lines([span("dup", name="one"), span("dup", name="two")]))
        assert "one" in out and "two" in out

    def test_deep_chain_does_not_hit_the_recursion_limit(self):
        spans = [span("s0")] + [span(f"s{i}", f"s{i - 1}") for i in range(1, 3000)]
        assert "3000 spans" in lines(spans)[0]

    def test_spans_without_times_still_render(self):
        out = "\n".join(lines([span("root"), span("c", "root", name="untimed", start=None, end=None, duration=None)]))
        assert "untimed" in out

    def test_empty_input_renders_nothing(self):
        assert waterfall.render_waterfall([]) == ""

    def test_long_names_are_truncated_to_the_column(self):
        row = next(line for line in lines([span("root", name="n" * 80)]) if line.startswith("n"))
        assert "n" * (waterfall.NAME_WIDTH - 1) + "…" in row and "n" * waterfall.NAME_WIDTH not in row


class TestTimelineBar:
    def bar(self, *args):
        return waterfall._timeline_bar(*args)  # pylint: disable=protected-access

    def test_full_width_span(self):
        assert self.bar(0, 1000, 0, 1000) == "│" + "█" * 20 + "│"

    def test_second_half(self):
        assert self.bar(500, 500, 0, 1000) == "│" + "░" * 10 + "█" * 10 + "│"

    def test_tiny_span_still_gets_one_block(self):
        assert self.bar(0, 1, 0, 1000).count("█") == 1

    @pytest.mark.parametrize("args", [
        (900, 5000, 0, 1000),            # runs past the end of the trace
        (float("nan"), 100, 0, 1000),    # no start time
        (0, 0, 0, 0),                    # zero-length trace
        (0, None, 0, 1000),              # no duration
    ])
    def test_bar_is_always_20_cells(self, args):
        assert len(self.bar(*args)) == 22
