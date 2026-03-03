"""CLI entry point and output formatting."""

from __future__ import annotations

import difflib
import re
import sys
import time
from collections import defaultdict
from pathlib import Path

import click
from rich.console import Console
from rich.markup import escape

from .parser import GenerateBlock, parse_file
from .runner import (
    Edit,
    TestResult,
    compute_generate_fix,
    compute_static_fix,
    expand_generate,
    gen_needs_fix,
    preserve_original,
    results_for_gen,
    run_test,
)

console = Console(highlight=False)


# --- Output ---


def _print_failure(r: TestResult) -> None:
    t = r.test
    console.print(
        f"\n[red]FAIL[/]  [bold]{escape(t.name)}[/]  ([dim]{t.file}:{t.line_number}[/])"
    )
    if r.error:
        console.print(f"\n  [red]{escape(r.error)}[/]")
        if not r.actual_output:
            if t.command:
                console.print(f"\n  $ {escape(t.command.command)}")
            return
    if not t.command or r.passed:
        return
    console.print(f"\n  $ {escape(t.command.command)}\n")
    exp = [e.format() for e in t.command.expected]
    for line in difflib.unified_diff(
        exp, r.actual_output, fromfile="expected", tofile="actual", lineterm=""
    ):
        esc = escape(line)
        if line.startswith(("---", "+++", "@@")):
            console.print(f"  [dim]{esc}[/]")
        elif line.startswith("-"):
            console.print(f"  [red]{esc}[/]")
        elif line.startswith("+"):
            console.print(f"  [green]{esc}[/]")
        else:
            console.print(f"  {esc}")
    if r.actual_exit_code != t.command.expected_exit_code:
        console.print(
            f"  [red]expected exit code {t.command.expected_exit_code}, got {r.actual_exit_code}[/]"
        )


# --- Applying edits ---


def _apply_edits(file: str, edits: list[Edit]) -> None:
    """Apply line edits to a file, bottom-to-top to preserve line numbers."""
    path = Path(file)
    lines = path.read_text().split("\n")
    for start, end, new in sorted(edits, key=lambda e: e[0], reverse=True):
        lines[start:end] = new
    path.write_text(re.sub(r"\n{3,}", "\n\n", "\n".join(lines)))


# --- Fix / Interactive ---


def _do_fix(results: list[TestResult], all_generates: list[GenerateBlock]) -> int:
    fixed = 0
    edits_by_file: dict[str, list[Edit]] = defaultdict(list)
    file_lines: dict[str, list[str]] = {}

    def _lines(file: str) -> list[str]:
        if file not in file_lines:
            file_lines[file] = Path(file).read_text().split("\n")
        return file_lines[file]

    # Static fixes
    for r in results:
        if r.test.is_generated:
            continue
        edit = compute_static_fix(r, _lines(r.test.file))
        if edit:
            edits_by_file[r.test.file].append(edit)
            fixed += 1
            console.print(f"[yellow]FIXED[/]  {escape(r.test.name)}")

    # Generate fixes
    for gen in all_generates:
        all_gen = results_for_gen(gen, results)
        non_stale = {r.test.name: r for r in all_gen if not r.test.stale}
        if not gen_needs_fix(gen, non_stale):
            continue
        edit = compute_generate_fix(gen, non_stale, _lines(gen.file))
        edits_by_file[gen.file].append(edit)
        fixed += 1
        for r in non_stale.values():
            console.print(f"[yellow]FIXED[/]  {escape(r.test.name)}")

    for file, edits in edits_by_file.items():
        _apply_edits(file, edits)

    return fixed


def _do_interactive(
    results: list[TestResult], all_generates: list[GenerateBlock]
) -> int:
    # Phase 1: prompt for each failure
    accepted: set[int] = set()
    for r in results:
        if r.passed:
            continue
        _print_failure(r)
        console.print("  [bold]\\[a]ccept / \\[s]kip / \\[q]uit[/] ? ", end="")
        try:
            ch = click.getchar().lower()
            console.print()
        except (OSError, EOFError):
            ch = "s"
        if ch == "a":
            accepted.add(id(r))
            console.print("  [yellow]ACCEPTED[/]")
        elif ch == "q":
            break

    if not accepted:
        return 0

    # Phase 2: collect and apply edits bottom-to-top per file
    fixed = 0
    edits_by_file: dict[str, list[Edit]] = defaultdict(list)
    file_lines: dict[str, list[str]] = {}

    def _lines(file: str) -> list[str]:
        if file not in file_lines:
            file_lines[file] = Path(file).read_text().split("\n")
        return file_lines[file]

    # Static fixes
    for r in results:
        if r.test.is_generated or id(r) not in accepted:
            continue
        edit = compute_static_fix(r, _lines(r.test.file))
        if edit:
            edits_by_file[r.test.file].append(edit)
            fixed += 1

    # Generate fixes
    for gen in all_generates:
        all_gen = results_for_gen(gen, results)
        if not any(id(r) in accepted for r in all_gen):
            continue
        patched: dict[str, TestResult] = {}
        for r in all_gen:
            name = r.test.name
            if r.test.stale:
                # Accepted stale = remove; skipped stale = preserve original
                if id(r) not in accepted:
                    orig = preserve_original(r, gen)
                    if orig:
                        patched[name] = orig
            elif id(r) in accepted:
                patched[name] = r
                fixed += 1
            else:
                # Skipped non-stale: preserve original expected output
                orig = preserve_original(r, gen)
                if orig:
                    patched[name] = orig
        edit = compute_generate_fix(gen, patched, _lines(gen.file))
        edits_by_file[gen.file].append(edit)

    for file, edits in edits_by_file.items():
        _apply_edits(file, edits)

    return fixed


def _print_summary(results: list[TestResult], elapsed: float, fixed: int) -> None:
    passed = sum(1 for r in results if r.passed)
    failed = len(results) - passed
    generated = sum(1 for r in results if r.test.is_generated)
    parts = []
    if passed:
        parts.append(f"[green]{passed} passed[/]")
    if failed:
        parts.append(f"[red]{failed} failed[/]")
    if generated:
        parts.append(f"{generated} generated")
    console.print(f"\n{', '.join(parts)} ({len(results)} total) in {elapsed:.2f}s")
    if fixed:
        console.print(f"[yellow]{fixed} test(s) fixed.[/]")


# --- Entry point ---


def _find_om_files(paths: tuple[str, ...]) -> list[str]:
    paths = paths or (".",)
    result = []
    for p in paths:
        path = Path(p)
        if path.is_file() and path.suffix == ".om":
            result.append(str(path))
        elif path.is_dir():
            result.extend(str(f) for f in sorted(path.rglob("*.om")))
    return result


@click.command()
@click.argument("files", nargs=-1)
@click.option("-i", "--interactive", is_flag=True, help="Review failures interactively")
@click.option("--fix", is_flag=True, help="Accept actual output as expected")
def main(files: tuple[str, ...], interactive: bool, fix: bool) -> None:
    om_files = _find_om_files(files)
    if not om_files:
        console.print("No .om files found.")
        sys.exit(0)

    all_tests, all_generates = [], []
    for f in om_files:
        tests, generates = parse_file(f)
        all_tests.extend(tests)
        all_generates.extend(generates)
    for gen in all_generates:
        all_tests.extend(expand_generate(gen))

    if not all_tests:
        console.print("No tests found.")
        sys.exit(0)

    start = time.monotonic()
    results = [run_test(t) for t in all_tests]
    elapsed = time.monotonic() - start

    if fix:
        fixed = _do_fix(results, all_generates)
    elif interactive:
        fixed = _do_interactive(results, all_generates)
    else:
        fixed = 0
        for r in results:
            if not r.passed:
                _print_failure(r)

    _print_summary(results, elapsed, fixed)
    sys.exit(1 if not fix and any(not r.passed for r in results) else 0)
