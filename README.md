# client-mod-bench

[![CI](https://github.com/rootpoii/client-mod-bench/actions/workflows/ci.yml/badge.svg)](https://github.com/rootpoii/client-mod-bench/actions/workflows/ci.yml)

Benchmarks Discord client mods: RAM, CPU and startup time, measured the same way every time so
you can actually compare them.

I made this while working on [Endcord](https://github.com/Endcord/Endcord). Every client mod
says it's lightweight (mine too), but nobody posts numbers you can rerun yourself. This
launches the client a few times, tracks all of its processes, closes it again and gives you
medians plus the raw data as JSON.

It doesn't inject anything, so it works with vanilla Discord, Vencord, Equicord,
BetterDiscord, Endcord, Vesktop or any other Electron app.

## Install

Needs Python 3.10+.

```bash
pipx install git+https://github.com/rootpoii/client-mod-bench
```

You get `client-mod-bench` and `cmbench`, same thing.

## Usage

```bash
# vanilla first (uninject your mod)
cmbench run -l vanilla -n 5 --discard-first

# inject the mod, then
cmbench run -l endcord -n 5 --discard-first

cmbench compare results/ -b vanilla    # markdown table
cmbench report results/ -b vanilla     # report.html with graphs
```

Results are saved to `./results`. Runs with the same label get merged, so you don't have to do
everything in one sitting.

Add `--cdp` to also get time to usable (until the server list shows up) and JS heap after a
GC, both read through DevTools.

Quick test on Canary, 2 runs of 60s:

```
$ cmbench run -t discord-canary -l canary-vanilla -n 2 -d 60 -w 25 --cdp
canary-vanilla: ...\DiscordCanary\app-1.0.1320\DiscordCanary.exe
2 run(s) of 60s, idle window starts at 25s
run 1: idle RAM 503 MB, idle CPU 30.63%, idle after 34.01 s, usable after 3.47 s, JS heap 98.2 MB
cooling down for 10s
run 2: idle RAM 467 MB, idle CPU 6.83%, idle after 19.01 s, usable after 3.21 s, JS heap 97.8 MB
canary-vanilla (median of 2 runs): idle RAM 485 MB, idle CPU 18.73%, idle after 26.51 s, usable after 3.34 s, JS heap 98.0 MB
saved results\canary-vanilla-20261111-071742.json

$ cmbench compare results/ --spread
```

| Metric | canary-vanilla |
| :--- | ---: |
| Idle RAM | 485 MB ±25.4 MB |
| Renderer RAM | 344 MB ±24.5 MB |
| Peak RAM | 678 MB ±23.9 MB |
| JS heap after GC | 98.0 MB ±0.2 MB |
| Idle CPU (one core) | 18.73% ±16.83% |
| Launch to usable | 3.34 s ±0.19 s |
| Launch to idle | 26.51 s ±10.60 s |
| Startup CPU time | 24.17 s ±12.33 s |
| Processes | 6 ±0 |
| Runs | 2 |

Run 1 was still loading stuff at 34s, so its idle CPU is way off. That's why the defaults are
90s runs with a 30s warmup, and you really want 5 runs, not 2.

## What it measures

- Idle RAM: USS of all the client's processes added up, median over the idle window (30s
  until the end of the run by default). USS so shared memory isn't counted six times. Falls
  back to RSS if USS can't be read.
- Renderer RAM: same, renderer processes only.
- Peak RAM: highest total during the run, startup included.
- Idle CPU: in % of one core, so 100% means one core fully busy.
- Launch to idle: when CPU drops under 5% and stays there for 5s (`--settle-cpu`,
  `--settle-window`).
- Startup CPU time: CPU seconds used until then. Roughly what one startup costs.
- Launch to usable / JS heap after GC: only with `--cdp`.

## Other stuff

```bash
cmbench targets                                   # which Discord builds it found
cmbench run -l ptb -t discord-ptb                 # discord, discord-ptb, discord-canary, vesktop
cmbench run -l mine --exe path/to/App.exe         # anything else
cmbench attach -l daily -p Discord -d 120         # already open client, doesn't close it
```

`--exe` only follows the process you started and its children. If the app hands off to some
other process, add `-p ProcessName`. Arguments after `--` go straight to the client.
`cmbench run --help` has everything else.

## Tips

- Same Discord build for every label. Discord updates itself on launch. If that happens
  mid-run the run gets a note, and `--discard-first` usually eats the update anyway.
- End each run on the same channel. Discord reopens the last one.
- Close other apps, plug in the laptop, don't touch the PC while it runs.
- If two medians are closer than the `--spread` value, it's noise.
- Post the JSON files with your table so people can check them.

## Notes

- I've only tested it on Windows 10 so far. The macOS and Linux paths are in there but I
  haven't tried them, PRs welcome.
- It won't start if Discord is already open (`--kill-existing` closes it for you), and it
  closes Discord after every run.
- `--cdp` opens a debugging port on localhost during the run. Anything on your PC could connect
  to it while you're logged in, so only use it on your own machine.
- If a run fails or you Ctrl+C, the runs that finished are still saved.

## Dev

```bash
pip install -e .
python -m unittest discover -s tests
```

The tests use a small fake process tree (`tests/fixtures/busy_tree.py`), no Discord needed.

## License

MIT
