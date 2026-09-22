"""CLI for the external zone: land URLs before the demo, list or remove what landed.

    python -m backend.ingestion.external https://example.org/rapor.xlsx --hint "konut kredisi"
    python -m backend.ingestion.external --list
    python -m backend.ingestion.external --remove <source_id>

Like every other ingestion CLI here it needs no rebuild afterwards -- and
unlike them it also needs no build step at all: the views the build created
read the new Parquet files on their next query.
"""
import argparse
import sys

from ...lakehouse import external_store as store
from . import ingest_url


def _print(result, indent: str = "") -> None:
    flag = "cache" if result.cache_hit else result.status
    print(f"{indent}[{flag:>7}] {result.source_id} {result.url} ({result.kind}, {result.extraction_route}): "
          f"{result.n_series} series, {result.n_observations} monthly rows")
    for key in result.series_keys[:12]:
        print(f"{indent}          {key}")
    if len(result.series_keys) > 12:
        print(f"{indent}          ... {len(result.series_keys) - 12} more")
    for warning in result.warnings[:6]:
        print(f"{indent}          ! {warning}")
    if result.error:
        print(f"{indent}          ! {result.error}")
    for child in result.children:
        _print(child, indent + "  ")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("urls", nargs="*", help="URLs to land (Excel/CSV/PDF/HTML/image, or a page linking to them)")
    parser.add_argument("--hint", default="", help="the question the source should answer; ranks linked documents")
    parser.add_argument("--force", action="store_true", help="re-land even when the bytes are unchanged")
    parser.add_argument("--no-follow", action="store_true", help="do not follow links from a landing page")
    parser.add_argument("--no-model", action="store_true", help="skip model labelling of unknown units")
    parser.add_argument("--list", action="store_true", help="show landed sources")
    parser.add_argument("--remove", metavar="SOURCE_ID", help="delete one landed source")
    args = parser.parse_args(argv)

    if args.list:
        sources = store.list_sources()
        if sources.empty:
            print("no external sources landed yet")
        for row in sources.itertuples():
            print(f"{row.source_id}  {row.status:>7}  {row.n_series:>4} series  {row.fetched_at}  {row.url}")
        return 0
    if args.remove:
        print("removed" if store.remove_source(args.remove) else "not found")
        return 0
    if not args.urls:
        parser.error("give at least one URL, or --list / --remove")

    client = None
    if not args.no_model:
        try:
            from ...core.config import kloudeks_api_key
            from ...llm import KloudeksClient
            kloudeks_api_key()
            client = KloudeksClient()
        except RuntimeError:
            client = None                                              # no key: heuristics + cross-check only

    failed = 0
    for url in args.urls:
        try:
            _print(ingest_url(url, args.hint or None, force=args.force, follow_links=not args.no_follow,
                              client=client))
        except Exception as exc:                                           # noqa: BLE001 -- report, continue
            failed += 1
            print(f"[  error] {url}: {type(exc).__name__}: {exc}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
