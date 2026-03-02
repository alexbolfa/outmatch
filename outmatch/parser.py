"""Parser for .om test files."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class ExpectedLine:
    text: str
    mode: str = "exact"  # "exact", "regex", or "glob"


@dataclass
class Command:
    line_number: int
    command: str
    expected: list[ExpectedLine] = field(default_factory=list)
    expected_exit_code: int = 0
    end_line_number: int = 0  # 1-indexed line after last command line (for multiline)


@dataclass
class TestCase:
    name: str
    file: str
    line_number: int
    command: Command | None = None
    is_generated: bool = False
    stale: bool = False
    missing_output: bool = False


@dataclass
class GenerateBlock:
    pipeline: str
    var_name: str
    name_template: str
    template_command: Command | None = None
    children: list[GenerateBlock] = field(default_factory=list)
    file: str = ""
    line_number: int = 0
    inline_results: dict[str, tuple[list[ExpectedLine], int]] = field(
        default_factory=dict
    )
    results_start_line: int = 0
    results_end_line: int = 0


_FOREACH_RE = re.compile(r"^(.*?)\s*\|\s*@foreach\s+(.+)$")
_HEADER_RE = re.compile(r"^##\s+(.+)$")
_EXIT_RE = re.compile(r"^exit\s+(\d+)$")
_REGEX_RE = re.compile(r"^/(.+)/$")
_VAR_RE = re.compile(r"@[A-Z][A-Z0-9_]*|\$[A-Za-z_][A-Za-z0-9_]*")


def _parse_foreach(text: str) -> tuple[str, str] | None:
    if m := _FOREACH_RE.match(text):
        return m.group(1).strip(), m.group(2).strip()
    return None


def _extract_var(template: str) -> str:
    if m := _VAR_RE.search(template):
        return m.group(0)
    return "@ITEM"


class _OMParser:
    def __init__(self, text: str, filename: str):
        self.lines = text.split("\n")
        self.filename = filename
        self.i = 0
        self.tests: list[TestCase] = []
        self.generates: list[GenerateBlock] = []

    def _consume_continuation(self, first_line: str) -> str:
        """Join backslash-continued lines into a single command string."""
        parts = [first_line]
        while parts[-1].endswith("\\") and self.i < len(self.lines):
            parts[-1] = parts[-1][:-1]
            parts.append(self.lines[self.i].strip())
            self.i += 1
        return "".join(parts)

    def parse(self) -> tuple[list[TestCase], list[GenerateBlock]]:
        while self.i < len(self.lines):
            line = self.lines[self.i]
            if not line.strip() or (line.startswith("#") and not line.startswith("##")):
                self.i += 1
                continue
            if m := _HEADER_RE.match(line):
                self.i += 1
                self._parse_block(m.group(1).strip())
                continue
            self.i += 1
        return self.tests, self.generates

    def _parse_block(self, name: str):
        while self.i < len(self.lines):
            line = self.lines[self.i]
            if not line.strip() or line.startswith("  #"):
                self.i += 1
                continue
            if not line.startswith("  $ "):
                break
            cmd_line_number = self.i + 1
            self.i += 1
            cmd_text = self._consume_continuation(line[4:])
            if foreach := _parse_foreach(cmd_text):
                self._parse_generate(foreach, indent=2, line_number=cmd_line_number)
            else:
                test = TestCase(name=name, file=self.filename, line_number=cmd_line_number)
                test.command = Command(line_number=cmd_line_number, command=cmd_text, end_line_number=self.i + 1)
                self._consume_expected(test.command, prefix="  ")
                self.tests.append(test)
            return

    def _parse_generate(self, foreach: tuple[str, str], indent: int, line_number: int):
        gen = GenerateBlock(
            pipeline=foreach[0],
            var_name=_extract_var(foreach[1]),
            name_template=foreach[1],
            file=self.filename,
            line_number=line_number,
        )
        self._parse_generate_body(gen, indent + 2)
        gen.results_start_line = self.i
        self._parse_inline_results(gen, indent)
        gen.results_end_line = self.i
        self.generates.append(gen)

    def _parse_generate_body(self, gen: GenerateBlock, indent: int):
        prefix = " " * indent + "$ "
        while self.i < len(self.lines):
            line = self.lines[self.i]
            if not line.strip():
                self.i += 1
                continue
            if not line.startswith(" " * indent):
                break
            if line.startswith(prefix):
                cmd_line_number = self.i + 1
                self.i += 1
                cmd_text = self._consume_continuation(line[len(prefix):])
                if foreach := _parse_foreach(cmd_text):
                    child = GenerateBlock(
                        pipeline=foreach[0],
                        var_name=_extract_var(foreach[1]),
                        name_template=foreach[1],
                        file=self.filename,
                        line_number=cmd_line_number,
                    )
                    self._parse_generate_body(child, indent + 2)
                    gen.children.append(child)
                    continue
                gen.template_command = Command(line_number=cmd_line_number, command=cmd_text, end_line_number=self.i + 1)
                self._consume_expected(gen.template_command, prefix=" " * (indent + 2))
                continue
            if not line.startswith(" " * (indent + 1)):
                break
            self.i += 1

    def _parse_inline_results(self, gen: GenerateBlock, indent: int):
        item_indent = " " * indent
        output_indent = " " * (indent + 2)
        while self.i < len(self.lines):
            line = self.lines[self.i]
            if not line.strip():
                self.i += 1
                continue
            if not line.startswith(item_indent) or line.startswith(item_indent + "  "):
                break
            if line.startswith(item_indent + "$ "):
                break
            item = line[len(item_indent) :]
            self.i += 1
            # Use a temporary Command to collect expected lines
            tmp = Command(line_number=0, command="")
            self._consume_expected(tmp, prefix=output_indent)
            gen.inline_results[item] = (tmp.expected, tmp.expected_exit_code)

    def _consume_expected(self, target: Command, prefix: str):
        while self.i < len(self.lines):
            line = self.lines[self.i]
            if not line.strip():
                j = self.i + 1
                while j < len(self.lines) and not self.lines[j].strip():
                    j += 1
                if j < len(self.lines) and self.lines[j].startswith(prefix):
                    target.expected.append(ExpectedLine(text=""))
                    self.i += 1
                    continue
                break
            if not line.startswith(prefix):
                break
            content = line[len(prefix) :]
            if m := _EXIT_RE.match(content):
                target.expected_exit_code = int(m.group(1))
            elif m := _REGEX_RE.match(content):
                target.expected.append(ExpectedLine(text=m.group(1), mode="regex"))
            elif content.startswith("glob: "):
                target.expected.append(ExpectedLine(text=content[6:], mode="glob"))
            else:
                target.expected.append(ExpectedLine(text=content))
            self.i += 1


def parse_file(path: str | Path) -> tuple[list[TestCase], list[GenerateBlock]]:
    path = Path(path)
    return _OMParser(path.read_text(), str(path)).parse()
