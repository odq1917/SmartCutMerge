import argparse
import re
import sys

import engine


def parse_input(arg):
    path, sep, spec = arg.rpartition("@")
    if not sep or not re.fullmatch(r"[\d:.,\-]+", spec):
        return {"path": arg, "cuts": []}
    cuts = []
    for item in spec.split(","):
        a, _, b = item.partition("-")
        cuts.append((engine.parse_time(a) or 0.0, engine.parse_time(b)))
    return {"path": path, "cuts": cuts}


def cli(argv):
    ap = argparse.ArgumentParser(
        prog="SmartCutMerge",
        description="Cut ranges out of videos and join them. Only the cut edges are re-encoded.")
    ap.add_argument("output")
    ap.add_argument("inputs", nargs="+", metavar="input[@start-end[,start-end...]]",
                    help="ranges to REMOVE, e.g. part1.mp4@1:02:10-1:02:40 part2.mp4")
    a = ap.parse_args(argv)
    try:
        engine.merge([parse_input(x) for x in a.inputs], a.output,
                     on_progress=lambda f: print(f"\r{f * 100:5.1f}%", end="", flush=True),
                     on_log=lambda m: print("\n" + m))
    except engine.CutError as e:
        print("\n" + str(e), file=sys.stderr)
        return 1
    print()
    return 0


if __name__ == "__main__":
    if len(sys.argv) > 1:
        sys.exit(cli(sys.argv[1:]))
    import gui
    gui.run()
