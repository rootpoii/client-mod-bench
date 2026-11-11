# Changelog

Format loosely follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## 0.1.0 (unreleased)

First version.

- `run`: starts a client a few times and records CPU, USS/RSS and memory per process type for
  its whole process tree (plus anything matched by name)
- launch to idle and startup CPU time, from the CPU timeline
- `--cdp`: launch to usable and JS heap after GC over the DevTools protocol
- `attach`: measure a client that's already open, without touching it
- `compare`: Markdown/text/JSON tables with % differences to a baseline
- `report`: single-file HTML report with memory and CPU charts
- `targets`: finds Discord Stable/PTB/Canary and Vesktop
- warnings when results come from different machines, settings, power states or sessions
  that stopped early
- handles Discord updating itself between or during runs (the path is looked up again before
  every launch, and affected runs get a note)
- runs that finished are saved even if a later one fails or you hit Ctrl+C
