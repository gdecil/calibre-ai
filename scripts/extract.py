import argparse

from app.extractor import run


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--chunk-size", type=int, default=6000)
    parser.add_argument("--overlap", type=int, default=800)

    args = parser.parse_args()

    run(args.limit)


if __name__ == "__main__":
    main()