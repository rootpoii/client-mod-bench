"""Stand-in for an Electron client: a two-process tree that works hard, then idles.

Usage: busy_tree.py [burn_seconds]
       busy_tree.py relaunch

The parent holds ~24 MB of memory. Both processes burn CPU for ``burn_seconds``
(default 2), then sleep until the benchmark closes them, or for at most a minute.

``relaunch`` imitates a client that restarts itself after an update: it starts a successor
process and exits after a second.
"""

import subprocess
import sys
import time


def burn(seconds: float) -> int:
    end = time.monotonic() + seconds
    spins = 0
    while time.monotonic() < end:
        spins += 1
    return spins


def main() -> None:
    args = sys.argv[1:]
    if args[:1] == ["relaunch"]:
        subprocess.Popen([sys.executable, __file__, "child", "0.5"])
        time.sleep(1)
        return
    is_child = args[:1] == ["child"]
    if is_child:
        args = args[1:]
    seconds = float(args[0]) if args else 2.0
    child = None
    if not is_child:
        child = subprocess.Popen([sys.executable, __file__, "child", str(seconds)])
        ballast = b"x" * (24 * 1024 * 1024)
        assert ballast
    burn(seconds)
    time.sleep(60)
    if child is not None:
        child.wait()


if __name__ == "__main__":
    main()
