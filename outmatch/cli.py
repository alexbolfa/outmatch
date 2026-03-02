"""CLI: run tests, format output, fix mode."""

from __future__ import annotations

import argparse
import difflib
import fnmatch
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

from .parser import Command, ExpectedLine, GenerateBlock, TestCase, parse_file


@dataclass
class TestResult:
    test: TestCase
    actual_output: list[str] = field(default_factory=list)
    actual_exit_code: int = 0
    passed: bool = True
    error: str | None = None


# --- Colors ---

_COLOR = not os.environ.get("NO_COLOR") and hasattr(sys.stdout, "isatty") and sys.stdout.isatty()

def _c(code: int, s: str) -> str:
    return f"\033[{code}m{s}\033[0m" if _COLOR else s


# --- Running ---

def _compare(expected: list[ExpectedLine], actual: list[str]) -> bool:
    if not expected:
        return not actual
    if any(e.mode != "exact" for e in expected):
        full = "\n".join(actual)
        return all(
            (re.search(e.text, full) if e.mode == "regex"
             else fnmatch.fnmatch(full, e.text) if e.mode == "glob"
             else e.text in actual)
            for e in expected
        )
    return len(expected) == len(actual) and all(e.text == a for e, a in zip(expected, actual))


def _expand_generate(
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
        proc = subprocess.run(pipeline, shell=True, capture_output=True, text=True, timeout=30)
        items = [line for line in proc.stdout.splitlines() if line.strip()]
    except (subprocess.TimeoutExpired, OSError):
        tests.append(TestCase(f"generate-error: {gen.name_template}", gen.file, root_line, is_generated=True))
        return tests

    for item in items:
        new_bindings = {**bindings, gen.var_name: item}
        parts = name_parts + [item]

        for child in gen.children:
            tests.extend(_expand_generate(child, new_bindings, parts, root_line))

        if gen.template_command:
            tmpl = gen.template_command
            cmd_str = tmpl.command
            for var, val in new_bindings.items():
                cmd_str = cmd_str.replace(var, val)
            tc = TestCase(
                name=" > ".join(parts) if len(parts) > 1 else gen.name_template.replace(gen.var_name, item),
                file=gen.file, line_number=root_line, is_generated=True,
                command=Command(
                    line_number=tmpl.line_number, command=cmd_str,
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
            tests.append(TestCase(name, gen.file, root_line, is_generated=True, stale=True))

    return tests


def _run_test(test: TestCase) -> TestResult:
    r = TestResult(test=test)
    if test.stale:
        r.passed = False
        r.error = "stale: item no longer in pipeline output (run 'outmatch --fix')"
        return r
    if test.missing_output:
        r.passed = False
        r.error = "new item: no expected output yet (run 'outmatch --fix')"
    if not test.command:
        return r
    try:
        proc = subprocess.run(
            test.command.command, shell=True, capture_output=True, text=True, timeout=60,
        )
    except subprocess.TimeoutExpired:
        r.actual_output = ["<timeout after 60s>"]
        r.actual_exit_code = -1
        r.passed = False
        return r
    r.actual_output = proc.stdout.splitlines() if proc.stdout else []
    r.actual_exit_code = proc.returncode
    if not _compare(test.command.expected, r.actual_output) or proc.returncode != test.command.expected_exit_code:
        r.passed = False
    return r


# --- Output ---

def _print_failure(r: TestResult) -> None:
    t = r.test
    print(f"\n{_c(31, 'FAIL')}  {_c(1, t.name)}  ({_c(2, f'{t.file}:{t.line_number}')})")
    if r.error:
        print(f"\n  {_c(31, r.error)}")
        return
    if not t.command or r.passed:
        return
    print(f"\n  $ {t.command.command}\n")
    exp = [e.text if e.mode == "exact" else f"/{e.text}/" if e.mode == "regex" else f"glob: {e.text}"
           for e in t.command.expected]
    for line in difflib.unified_diff(exp, r.actual_output, fromfile="expected", tofile="actual", lineterm=""):
        if line.startswith(("---", "+++", "@@")):
            print(f"  {_c(2, line)}")
        elif line.startswith("-"):
            print(f"  {_c(31, line)}")
        elif line.startswith("+"):
            print(f"  {_c(32, line)}")
        else:
            print(f"  {line}")
    if r.actual_exit_code != t.command.expected_exit_code:
        print(f"  {_c(31, f'expected exit code {t.command.expected_exit_code}, got {r.actual_exit_code}')}")


# --- Fix ---

def _fix_static(r: TestResult) -> bool:
    if r.test.is_generated or not r.test.command or r.passed:
        return False
    path = Path(r.test.file)
    lines = path.read_text().split('\n')
    start = r.test.command.end_line_number - 1  # 0-indexed line after last command line
    end = start
    while end < len(lines) and lines[end].strip() and lines[end].startswith('  ') and not lines[end].startswith('##'):
        end += 1
    new = [f"  {out}" for out in r.actual_output]
    if r.actual_exit_code != 0:
        new.append(f"  exit {r.actual_exit_code}")
    lines[start:end] = new
    path.write_text(re.sub(r'\n{3,}', '\n\n', '\n'.join(lines)))
    return True


def _fix_generate(gen: GenerateBlock, by_name: dict[str, TestResult]) -> bool:
    path = Path(gen.file)
    lines = path.read_text().split('\n')
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
    lines[gen.results_start_line:end] = new
    path.write_text('\n'.join(lines))
    return True


# --- Main ---

def _find_om_files(paths: list[str]) -> list[str]:
    if not paths:
        paths = ["."]
    result = []
    for p in paths:
        path = Path(p)
        if path.is_file() and path.suffix == ".om":
            result.append(str(path))
        elif path.is_dir():
            result.extend(str(f) for f in sorted(path.rglob("*.om")))
    return result


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="outmatch", description="Shell test runner")
    ap.add_argument("files", nargs="*")
    ap.add_argument("-i", dest="interactive", action="store_true", help="Review failures interactively")
    ap.add_argument("--fix", action="store_true", help="Accept actual output as expected")
    args = ap.parse_args(argv)

    files = _find_om_files(args.files)
    if not files:
        print("No .om files found.")
        sys.exit(0)

    all_tests: list[TestCase] = []
    all_generates: list[GenerateBlock] = []
    for f in files:
        tests, generates = parse_file(f)
        all_tests.extend(tests)
        all_generates.extend(generates)
    for gen in all_generates:
        all_tests.extend(_expand_generate(gen))

    if not all_tests:
        print("No tests found.")
        sys.exit(0)

    start = time.monotonic()
    results = [_run_test(t) for t in all_tests]
    elapsed = time.monotonic() - start

    fixed = 0
    if args.fix:
        for r in results:
            if not r.test.is_generated and _fix_static(r):
                fixed += 1
                print(f"{_c(33, 'FIXED')}  {r.test.name}")
        for gen in reversed(all_generates):
            by_name = {r.test.name: r for r in results
                       if r.test.is_generated and not r.test.stale
                       and r.test.file == gen.file and r.test.line_number == gen.line_number}
            if not by_name and not gen.inline_results:
                continue
            needs_fix = (not gen.inline_results and by_name) or (
                gen.inline_results and (
                    set(gen.inline_results) != set(by_name) or any(not r.passed for r in by_name.values())))
            if needs_fix and _fix_generate(gen, by_name):
                fixed += 1
                for r in by_name.values():
                    print(f"{_c(33, 'FIXED')}  {r.test.name}")
    else:
        accepted_generated: set[int] = set()  # id() of accepted generated TestResults
        for r in results:
            if not r.passed:
                _print_failure(r)
                if args.interactive and not r.error:
                    prompt = "  \033[1m[a]ccept / [s]kip / [q]uit\033[0m ? "
                    sys.stdout.write(prompt)
                    sys.stdout.flush()
                    try:
                        ch = open("/dev/tty").readline().strip().lower()
                    except OSError:
                        ch = "s"
                    if ch == "a":
                        if r.test.is_generated:
                            accepted_generated.add(id(r))
                            fixed += 1
                            print(f"  {_c(33, 'ACCEPTED')}")
                        elif _fix_static(r):
                            fixed += 1
                            print(f"  {_c(33, 'ACCEPTED')}")
                    elif ch == "q":
                        break
        if accepted_generated:
            for gen in reversed(all_generates):
                by_name = {r.test.name: r for r in results
                           if r.test.is_generated and not r.test.stale
                           and r.test.file == gen.file and r.test.line_number == gen.line_number}
                if not by_name:
                    continue
                if not any(id(r) in accepted_generated for r in by_name.values()):
                    continue
                # For non-accepted tests, use their original expected output
                # so _fix_generate doesn't overwrite them with actual output
                patched = {}
                for name, r in by_name.items():
                    if id(r) in accepted_generated:
                        patched[name] = r
                    elif name in gen.inline_results:
                        ir = gen.inline_results[name]
                        exp_lines = [e.text if e.mode == "exact"
                                     else f"/{e.text}/" if e.mode == "regex"
                                     else f"glob: {e.text}" for e in ir[0]]
                        patched[name] = TestResult(test=r.test, actual_output=exp_lines,
                                                   actual_exit_code=ir[1])
                _fix_generate(gen, patched)

    passed = sum(1 for r in results if r.passed)
    failed = len(results) - passed
    generated = sum(1 for r in results if r.test.is_generated)
    parts = []
    if passed:
        parts.append(_c(32, f"{passed} passed"))
    if failed:
        parts.append(_c(31, f"{failed} failed"))
    if generated:
        parts.append(f"{generated} generated")
    print(f"\n{', '.join(parts)} ({len(results)} total) in {elapsed:.2f}s")
    if fixed:
        print(f"{_c(33, f'{fixed} test(s) fixed.')}")

    sys.exit(1 if not args.fix and failed else 0)
