# Engine report fixtures for builtin:mutants

Small samples of what each mutation engine writes, used by tests/test_mutants.py
to test the parsers. Which engine version each one imitates:

| Folder | Engine | How it was made |
|---|---|---|
| `mutmut/` | mutmut 3.8.1 | Captured from a real run (Ubuntu 24.04 in WSL, Python 3.12): `mutants/<file>.meta`, `mutmut show <mutant>` and `mutmut results --all true`. `counter.py.txt` and `shelf.py.txt` are the sources it ran on. |
| `stryker-js/` | StrykerJS 10.1 | Hand-written from the mutation-testing-elements schema (`reports/mutation/mutation.json`). `price.ts.txt` is the source. |
| `stryker-net/` | Stryker.NET 5.0 | Hand-written in the same schema (`<output>/reports/mutation-report.json`). Stryker.NET keys files by absolute path; the test replaces `__TREE__` with its own folder. |
| `cargo-mutants/` | cargo-mutants 27.1 | Hand-written `mutants.out/missed.txt`, `caught.txt` and `timeout.txt` (`path:line:col: description`). |
| `gremlins/` | Gremlins 0.6 | Hand-written `gremlins unleash -o` JSON. |

The hand-written JSON files carry an `_imitates` key; the engines' parsers ignore
unknown keys.
