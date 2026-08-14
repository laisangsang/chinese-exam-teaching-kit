import argparse


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="cekit")
    parser.add_argument("--version", action="store_true")
    args = parser.parse_args(argv)
    if args.version:
        print("cekit 0.1.0")
    return 0
