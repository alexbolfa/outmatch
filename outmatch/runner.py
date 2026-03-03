"""Test execution, comparison, and fixing."""

from __future__ import annotations

import fnmatch
import re
import subprocess
from dataclasses import dataclass, field

from .parser import Command, ExpectedLine, GenerateBlock, TestCase


@dataclass
class TestResult:
    test: TestCase
    actual_output: list[str] = field(default_factory=list)
    actual_exit_code: int = 0
    passed: bool = True
    error: str | None = None


def compare(expected: list[ExpectedLine], actual: list[str]) -> bool:
    if not expected:
        return not actual
    if any(e.mode != "exact" for e in expected):
        full = "\n".join(actual)
        return all(
            (
                re.search(e.text, full)
                if e.mode == "regex"
                else fnmatch.fnmatch(full, e.text)
                if e.mode == "glob"
                else e.text in actual
            )
            for e in expected
        )
    return len(expected) == len(actual) and all(
        e.text == a for e, a in zip(expected, actual)
    )


def expand_generate(
    gen: GenerateBlock,
    bindings: dict[str, str] | None = None,
    name_parts: list[str] | None = None,
    root_line: int | None = None,
) -> list[TestCase]:
    bindings = bindings or {}
    name_parts = name_parts or []
    root_line = root_line or gen.line_number
    tests: list[TestCase] = []

    try:
        pipeline = gen.pipeline
        for var, val in bindings.items():
            pipeline = pipeline.replace(var, val)
        proc = subprocess.run(
            pipeline, shell=True, capture_output=True, text=True, timeout=30
        )
        items = [line for line in proc.stdout.splitlines() if line.strip()]
    except (subprocess.TimeoutExpired, OSError):
        tests.append(
            TestCase(
                f"generate-error: {gen.name_template}",
                gen.file,
                root_line,
                is_generated=True,
            )
        )
        return tests

    for item in items:
        new_bindings = {**bindings, gen.var_name: item}
        parts = name_parts + [item]

        for child in gen.children:
            tests.extend(expand_generate(child, new_bindings, parts, root_line))

        if gen.template_command:
            tmpl = gen.template_command
            cmd_str = tmpl.command
            for var, val in new_bindings.items():
                cmd_str = cmd_str.replace(var, val)
            tc = TestCase(
                name=" > ".join(parts)
                if len(parts) > 1
                else gen.name_template.replace(gen.var_name, item),
                file=gen.file,
                line_number=root_line,
                is_generated=True,
                command=Command(
                    line_number=tmpl.line_number,
                    command=cmd_str,
                    expected=[ExpectedLine(e.text, e.mode) for e in tmpl.expected],
                    expected_exit_code=tmpl.expected_exit_code,
                    end_line_number=tmpl.end_line_number,
                ),
            )
            tests.append(tc)

    if gen.inline_results:
        for tc in tests:
            ir = gen.inline_results.get(tc.name)
            if ir and tc.command and not tc.command.expected:
                tc.command.expected = [ExpectedLine(e.text, e.mode) for e in ir[0]]
                if ir[1]:
                    tc.command.expected_exit_code = ir[1]
            else:
                tc.missing_output = True
        test_names = {tc.name for tc in tests}
        for name in sorted(set(gen.inline_results) - test_names):
            stale_cmd = None
            if gen.template_command:
                tmpl = gen.template_command
                cmd_str = tmpl.command.replace(gen.var_name, name)
                stale_cmd = Command(
                    line_number=tmpl.line_number,
                    command=cmd_str,
                    expected_exit_code=tmpl.expected_exit_code,
                    end_line_number=tmpl.end_line_number,
                )
            tests.append(
                TestCase(name, gen.file, root_line, is_generated=True, stale=True, command=stale_cmd)
            )

    return tests


def run_test(test: TestCase) -> TestResult:
    r = TestResult(test=test)
    if test.stale:
        r.passed = False
        r.error = "Deleted test: Expected a previously generated test."
        return r
    if test.missing_output:
        r.passed = False
        r.error = "New test: No baseline output to compare against."
    if not test.command:
        return r
    try:
        proc = subprocess.run(
            test.command.command,
            shell=True,
            capture_output=True,
            text=True,
            timeout=60,
        )
    except subprocess.TimeoutExpired:
        r.actual_output = ["<timeout after 60s>"]
        r.actual_exit_code = -1
        r.passed = False
        return r
    r.actual_output = proc.stdout.splitlines() if proc.stdout else []
    r.actual_exit_code = proc.returncode
    if (
        not compare(test.command.expected, r.actual_output)
        or proc.returncode != test.command.expected_exit_code
    ):
        r.passed = False
    return r


# --- Fix computation ---

# An edit is (start_line, end_line, new_lines) applied to a file's line array.
Edit = tuple[int, int, list[str]]


def compute_static_fix(r: TestResult, lines: list[str]) -> Edit | None:
    if r.test.is_generated or not r.test.command or r.passed:
        return None
    start = r.test.command.end_line_number - 1
    end = start
    while (
        end < len(lines)
        and lines[end].strip()
        and lines[end].startswith("  ")
        and not lines[end].startswith("##")
    ):
        end += 1
    new = [f"  {out}" for out in r.actual_output]
    if r.actual_exit_code != 0:
        new.append(f"  exit {r.actual_exit_code}")
    return (start, end, new)


def compute_generate_fix(
    gen: GenerateBlock, by_name: dict[str, TestResult], lines: list[str]
) -> Edit:
    new: list[str] = []
    for name, r in by_name.items():
        new.append(f"  {name}")
        for out in r.actual_output:
            new.append(f"    {out}")
        if r.actual_exit_code != 0:
            new.append(f"    exit {r.actual_exit_code}")
    end = gen.results_end_line
    if new and end < len(lines) and lines[end].strip():
        new.append("")
    return (gen.results_start_line, end, new)


def results_for_gen(gen: GenerateBlock, results: list[TestResult]) -> list[TestResult]:
    return [
        r
        for r in results
        if r.test.is_generated
        and r.test.file == gen.file
        and r.test.line_number == gen.line_number
    ]


def gen_needs_fix(gen: GenerateBlock, by_name: dict[str, TestResult]) -> bool:
    if not by_name and not gen.inline_results:
        return False
    if not gen.inline_results:
        return bool(by_name)
    return set(gen.inline_results) != set(by_name) or any(
        not r.passed for r in by_name.values()
    )


def preserve_original(r: TestResult, gen: GenerateBlock) -> TestResult | None:
    """Build a TestResult with the original expected output from inline_results."""
    ir = gen.inline_results.get(r.test.name)
    if not ir:
        return None
    return TestResult(
        test=r.test,
        actual_output=[e.format() for e in ir[0]],
        actual_exit_code=ir[1],
    )
