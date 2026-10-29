"""Parsing !analyze -v output: the fields an agent acts on, and nothing invented."""

from __future__ import annotations

from pathlib import Path

from hyperv_console_mcp.kd.analyze import analyze_report

FIXTURE = Path(__file__).parent / "fixtures" / "analyze_d1.txt"


def sample() -> str:
    return FIXTURE.read_text(encoding="utf-8")


def test_a_driver_irql_bugcheck_is_fully_structured() -> None:
    report = analyze_report(sample())

    assert report["found"] is True
    assert report["bugcheck_code"] == "0xd1"
    assert report["bugcheck_code_int"] == 0xD1
    assert report["bugcheck_parameters"] == ["0x10", "0x2", "0x0", "0xfffff80512345678"]
    assert report["module_name"] == "MyDriver"
    assert report["image_name"] == "MyDriver.sys"
    assert report["failure_bucket_id"] == "AV_MyDriver!Crash"
    assert report["default_bucket_id"] == "WIN8_DRIVER_FAULT"
    assert report["process_name"] == "System"
    assert report["symbol_name"] == "MyDriver!Crash+18"


def test_the_blamed_driver_comes_from_the_probably_caused_by_line() -> None:
    report = analyze_report(sample())

    assert report["blamed_driver"] == "MyDriver.sys"
    assert report["probably_caused_by"] == "MyDriver.sys ( MyDriver!Crash+18 )"


def test_stack_frames_keep_the_symbol_and_drop_the_argument_noise() -> None:
    report = analyze_report(sample())

    frames = report["stack_frames"]
    assert [frame["symbol"] for frame in frames] == [
        "nt!KeBugCheckEx",
        "MyDriver!Crash+0x18",
        "nt!KiDispatchException+0x1a0",
        "nt!KiExceptionDispatch+0x150",
    ]
    # The driver frame is the second one, right after the bugcheck frame.
    assert frames[1]["address"] == "0xfffff8050a1b2c38"
    assert report["top_frames"] == frames


def test_an_unrelated_text_is_not_a_bugcheck_analysis() -> None:
    report = analyze_report("Microsoft (R) Windows Debugger Version 10.0\nkd> ")

    assert report["found"] is False
    assert "bugcheck_code" not in report
    assert "stack_frames" not in report


def test_a_stack_without_a_bugcheck_code_still_counts_as_an_analysis() -> None:
    text = (
        "STACK_TEXT:\n"
        "fffff805`0a1b2c30 fffff805`0d3e4f50 : 00000000`0000000a : nt!KeBugCheckEx\n"
    )

    report = analyze_report(text)

    assert report["found"] is True
    assert "bugcheck_code" not in report
    assert report["top_frames"][0]["symbol"] == "nt!KeBugCheckEx"


def test_addresses_lose_the_debugger_backtick() -> None:
    report = analyze_report("BUGCHECK_P1: ffff8888`88888888\n")

    assert report["bugcheck_parameters"] == ["0xffff888888888888"]
