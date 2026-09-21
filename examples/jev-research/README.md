# Synthetic research fixtures

All sources, entities, timestamps, facts and outcomes in this directory are
invented software fixtures. `example.org` is a placeholder; these files do not
represent live financial events, a real rewards program, real forecast accuracy,
or provider results.

Each CLI command has a corresponding JSON input. The eight primary workflows
also have `*-insufficient.json` inputs. `mocked-results.json` is a reviewable
record of running these inputs with injected, fabricated judgments. Its null
usage is intentional. It must never be cited as evidence that JEV is accurate.

```sh
python -m research_jev event --input examples/jev-research/event.json
python -m research_jev relevance --input examples/jev-research/relevance.json
python -m research_jev paper-evaluate --input examples/jev-research/paper-evaluate.json
```

The first two commands default to off and do not call TypeSafe.
`paper-evaluate` always runs offline. Missing-evidence commands return exit code
2 and an explicit status. Input and output semantics are described in
[the research runbook](../../docs/JEV-RESEARCH.md).
