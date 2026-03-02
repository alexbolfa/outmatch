# outmatch

**Out**put **match**ing for shell commands. A modern replacement for [Cram](https://bitheap.org/cram/) with a cleaner interface, dynamic test generation, multiple tests per file, and zero dependencies.

Write a test — just a command and its expected output:

<p align="center">
  <img src="assets/syntax.svg" width="620" alt="Example .om test file with syntax highlighting">
</p>

Run it:

<p align="center">
  <img src="assets/pass.svg" width="620" alt="All tests passing">
</p>

When something breaks, you get a clear diff:

<p align="center">
  <img src="assets/fail.svg" width="620" alt="Failing tests with colored diff output">
</p>

Fix it automatically — outmatch rewrites expected output in-place:

<p align="center">
  <img src="assets/fix.svg" width="620" alt="outmatch --fix accepting actual output">
</p>

## Install

```
pip install outmatch
```

Or with uv:

```
uv tool install outmatch
```

## CLI

```
outmatch [FILES...]           # Run tests (default: all *.om recursively)
outmatch --fix [FILES...]     # Accept actual output as expected
outmatch -i [FILES...]        # Interactive — review each failure
```

## File Format

Tests live in `.om` files. Each test starts with a `##` header, followed by a `$ command` and its expected output:

```
# Comments at column 0

## name of test
  $ command to run
  expected output line
  /regex pattern to match/
  glob: glob pattern to match
  exit 1
```

### Dynamic Test Generation

The `| @foreach` syntax generates tests from command output — one test per line:

```
## check each fruit
  $ cat fruits.txt | @foreach @FRUIT
    $ echo "I like @FRUIT"

  apple
    I like apple
  banana
    I like banana
```

Run `outmatch --fix` to capture output inline.

## How It Differs from Cram

|                    | Cram            | Outmatch                     |
| ------------------ | --------------- | ---------------------------- |
| Tests per file     | One             | Many (`##` headers)          |
| Dynamic generation | No              | `\| @foreach` pipelines      |
| Regex matching     | `re` suffix     | `/pattern/` delimiters       |
| Glob matching      | `glob` suffix   | `glob:` prefix               |
| Fix mode           | `--interactive` | `outmatch --fix` (automatic) |
| Generated output   | No              | Inline, per item             |
| Dependencies       | Python 2/3      | Python 3.10+, zero deps      |

## License

MIT
