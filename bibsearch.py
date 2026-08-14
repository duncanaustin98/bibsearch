#!/usr/bin/env python3
"""
Check whether sky position(s) have appeared in the astrophysics literature.

Workflow: optionally confirm archival coverage via MAST for a given mission,
then pull the bibliography directly from SIMBAD (biblio field) and NED
(references table) for any catalogued object at that position. VizieR can be
added with --vizier: CDS ingests published tables far more comprehensively than
they become SIMBAD/NED objects, so it reaches papers the other two miss, at the
cost of roughly a minute per position. ADS is used only as an optional
supplement/title-enrichment layer when a key is set -- searching ADS by object
name alone is unreliable, so it is not the primary source.

Every bibcode in the report is tagged with the service(s) that found it:
[S]IMBAD, [N]ED, [V]izieR, [A]DS.

Setup:
    pip install .            # or: pip install ".[ads]" for ADS enrichment
    export ADS_DEV_KEY="your-ads-api-token"   # https://ui.adsabs.harvard.edu/user/settings/token (optional)

Usage:
    bibsearch --radec 53.15398,-27.80095
    bibsearch --radec coords.txt
    bibsearch --radec 53.15398,-27.80095 --radius 3 --savefile out.txt
    bibsearch --radec 53.15398,-27.80095 --mission HST
    bibsearch --radec 53.15398,-27.80095 --vizier

coords.txt format (two comma-separated columns, RA,Dec in degrees, one pair per line):
    53.15398,-27.80095
    53.16,-27.81
"""

import argparse
import html
import os
import re
import sys
import warnings
from concurrent.futures import ThreadPoolExecutor, as_completed

from astropy.utils.exceptions import AstropyWarning
from astroquery.exceptions import NoResultsWarning

warnings.simplefilter("ignore", category=NoResultsWarning)
warnings.simplefilter("ignore", category=AstropyWarning)

import astropy.units as u
from astropy.coordinates import SkyCoord
from astroquery.exceptions import RemoteServiceError
from astroquery.mast import Observations
from astroquery.simbad import Simbad
from astroquery.ipac.ned import Ned
from astroquery.vizier import Vizier

try:
    import ads
except ImportError:
    # Optional: only used to enrich titles/authors, and only when ADS_DEV_KEY is set.
    ads = None

try:
    from tqdm import tqdm
except ImportError:
    tqdm = None

Simbad.add_votable_fields("biblio")

# A bibcode is exactly 19 characters and contains no whitespace. VizieR's
# origin_article field returns free text for some archive catalogues
# ("European Southern Observatory (2016)"), which this filters out.
BIBCODE_RE = re.compile(r"^\d{4}\S{15}$")

# Order in which provenance tags are displayed.
_TAG_ORDER = "SNVA"


def _fmt_exc(exc):
    return f"{type(exc).__name__}: {exc}"


def _plain_progress(iterable, desc, total):
    """Minimal stderr progress fallback used when tqdm is not installed."""
    for i, item in enumerate(iterable, 1):
        print(f"\r  {desc}: {i}/{total}", end="", file=sys.stderr, flush=True)
        yield item
    print("", file=sys.stderr, flush=True)


def _progress(iterable, desc, total, enabled):
    """Wrap an iterable in a progress bar written to stderr, so stdout stays clean."""
    if not enabled or not total:
        return iterable
    if tqdm is not None:
        return tqdm(
            iterable,
            desc=f"  {desc}",
            total=total,
            file=sys.stderr,
            leave=False,
            bar_format="{desc}: {n_fmt}/{total_fmt} |{bar:24}| {elapsed}",
        )
    return _plain_progress(iterable, desc, total)


def check_mission_coverage(coord, radius_arcsec, mission):
    return Observations.query_criteria(
        coordinates=coord,
        radius=radius_arcsec * u.arcsec,
        obs_collection=mission,
    )


def simbad_lookup(coord, radius_arcsec):
    """Return ({main_id: set(bibcodes)}, errors) pulled from SIMBAD's biblio field."""
    try:
        result = Simbad.query_region(coord, radius=radius_arcsec * u.arcsec)
    except Exception as e:
        return {}, [f"SIMBAD region query: {_fmt_exc(e)}"]
    if result is None:
        return {}, []

    hits = {}
    for row in result:
        name = str(row["main_id"])
        biblio = row["biblio"]
        bibcodes = set(str(biblio).split("|")) if biblio and str(biblio) not in ("", "--") else set()
        hits[name] = bibcodes
    return hits, []


def ned_lookup(coord, radius_arcsec, workers=8, show_progress=False):
    """Return ({name: {bibcode: (title, first_author)}}, errors) from NED's references tables."""
    try:
        result = Ned.query_region(coord, radius=radius_arcsec * u.arcsec)
    except Exception as e:
        return {}, [f"NED region query: {_fmt_exc(e)}"]
    if result is None:
        return {}, []

    names = [str(row["Object Name"]) for row in result]
    hits = {}
    errors = []

    def fetch(name):
        return Ned.get_table(name, table="references")

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(fetch, name): name for name in names}
        for future in _progress(as_completed(futures), "NED references", len(futures), show_progress):
            name = futures[future]
            try:
                refs = future.result()
            except RemoteServiceError:
                # NED signals "nothing to return for this object" with an error
                # response, so this is the normal empty case rather than a fault.
                hits[name] = {}
                continue
            except Exception as e:
                # Network, timeout or parse problems are genuine failures: record
                # them so an incomplete result is never mistaken for a clean miss.
                errors.append(f"NED references for {name}: {_fmt_exc(e)}")
                hits[name] = {}
                continue

            papers = {}
            for r in refs:
                title = html.unescape(str(r["Article Title"]))
                author = str(r["Author List"]).split(";")[0].strip()
                papers[str(r["Refcode"])] = (title, author)
            hits[name] = papers
    return hits, errors


def vizier_lookup(coord, radius_arcsec, workers=8, show_progress=False):
    """Return ({bibcode: (title, first_author)}, errors) for VizieR catalogues covering the position.

    VizieR indexes published tables directly, so it reaches papers whose sources
    were never folded into SIMBAD or NED as catalogued objects. Sub-tables are
    collapsed to their parent catalogue before metadata is fetched.
    """
    try:
        # row_limit=1: we only need to know that a catalogue covers this
        # position, never the photometry itself.
        result = Vizier(row_limit=1).query_region(coord, radius=radius_arcsec * u.arcsec)
    except Exception as e:
        return {}, [f"VizieR cone search: {_fmt_exc(e)}"]

    parents = sorted({
        "/".join(name.split("/")[:-1]) if name.count("/") > 1 else name
        for name in (t.meta.get("name") for t in result)
        if name
    })
    if not parents:
        return {}, []

    papers = {}
    errors = []

    def fetch(catalog):
        return Vizier().get_catalog_metadata(catalog=catalog)

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(fetch, cat): cat for cat in parents}
        for future in _progress(as_completed(futures), "VizieR metadata", len(futures), show_progress):
            catalog = futures[future]
            try:
                meta = future.result()
            except Exception as e:
                errors.append(f"VizieR metadata for {catalog}: {_fmt_exc(e)}")
                continue
            if meta is None or len(meta) == 0 or "origin_article" not in meta.colnames:
                continue

            row = meta[0]
            bibcode = str(row["origin_article"]).strip()
            if not BIBCODE_RE.match(bibcode):
                continue
            title = str(row["title"]).strip() if "title" in meta.colnames else ""
            authors = str(row["authors"]) if "authors" in meta.colnames else ""
            papers[bibcode] = (title or None, authors.split(";")[0].strip() or None)
    return papers, errors


def find_papers_for_object(name, rows=50):
    """Optional ADS supplement: search by resolved object name (needs ADS_DEV_KEY)."""
    if ads is None:
        return []
    try:
        return list(ads.SearchQuery(
            q=f'object:"{name}"',
            fl=["bibcode", "title", "author"],
            rows=rows,
        ))
    except Exception:
        return []


def enrich_metadata(bibcodes, chunk_size=50):
    """Optional ADS supplement: batch-fetch title/author for bibcodes missing them (needs ADS_DEV_KEY)."""
    metadata = {}
    if ads is None:
        return metadata
    bibcodes = list(bibcodes)
    for i in range(0, len(bibcodes), chunk_size):
        chunk = bibcodes[i : i + chunk_size]
        try:
            q = "identifier:(" + " OR ".join(chunk) + ")"
            for p in ads.SearchQuery(q=q, fl=["bibcode", "title", "author"], rows=len(chunk)):
                title = p.title[0] if p.title else None
                author = p.author[0] if p.author else None
                metadata[p.bibcode] = (title, author)
        except Exception:
            pass
    return metadata


def check_object_in_literature(
    ra_deg,
    dec_deg,
    radius_arcsec,
    mission,
    use_vizier=False,
    workers=8,
    show_progress=False,
):
    """Return a formatted report string for one RA/Dec pair."""
    lines = [f"RA={ra_deg:.6f}  Dec={dec_deg:.6f}  (radius={radius_arcsec}\")"]

    coord = SkyCoord(ra=ra_deg * u.deg, dec=dec_deg * u.deg, frame="icrs")
    errors = []

    target_names = set()
    if mission:
        try:
            mission_obs = check_mission_coverage(coord, radius_arcsec, mission)
        except Exception as e:
            mission_obs = []
            errors.append(f"MAST {mission} query: {_fmt_exc(e)}")

        target_names = set(mission_obs["target_name"]) if len(mission_obs) else set()
        mission_line = f"  {mission} observations: {len(mission_obs)}"
        if target_names:
            mission_line += f"  (target: {', '.join(sorted(target_names))})"
        lines.append(mission_line)

    simbad_hits, simbad_errors = simbad_lookup(coord, radius_arcsec)
    errors += simbad_errors

    ned_hits, ned_errors = ned_lookup(coord, radius_arcsec, workers, show_progress)
    errors += ned_errors

    vizier_papers, vizier_errors = ({}, [])
    if use_vizier:
        vizier_papers, vizier_errors = vizier_lookup(coord, radius_arcsec, workers, show_progress)
        errors += vizier_errors

    names = set(simbad_hits) | set(ned_hits) | target_names

    if names:
        lines.append(f"  Resolved name(s): {', '.join(sorted(names))}")
    else:
        lines.append("  Resolved name(s): none (uncatalogued position)")

    papers = {}  # bibcode -> {"title": ..., "author": ..., "sources": set()}

    def merge(bibcode, title=None, author=None, source=None):
        entry = papers.setdefault(bibcode, {"title": None, "author": None, "sources": set()})
        if title and not entry["title"]:
            entry["title"] = title
        if author and not entry["author"]:
            entry["author"] = author
        if source:
            entry["sources"].add(source)

    for bibcodes in simbad_hits.values():
        for bc in bibcodes:
            merge(bc, source="S")
    for refs in ned_hits.values():
        for bc, (title, author) in refs.items():
            # NED metadata takes priority (fetched directly, no key needed).
            merge(bc, title=title, author=author, source="N")
    for bc, (title, author) in vizier_papers.items():
        merge(bc, title=title, author=author, source="V")

    for name in set(simbad_hits) | set(ned_hits):
        for p in find_papers_for_object(name):
            title = p.title[0] if p.title else None
            author = p.author[0] if p.author else None
            merge(p.bibcode, title=title, author=author, source="A")

    missing = [bc for bc, v in papers.items() if not v["title"] or not v["author"]]
    if missing:
        for bc, (title, author) in enrich_metadata(missing).items():
            merge(bc, title=title, author=author)

    counts = {tag: sum(1 for v in papers.values() if tag in v["sources"]) for tag in _TAG_ORDER}
    breakdown = ", ".join(
        f"{label}: {counts[tag]}"
        for tag, label in zip(_TAG_ORDER, ("SIMBAD", "NED", "VizieR", "ADS"))
        if counts[tag]
    )
    summary = f"  Papers found: {len(papers)}"
    if breakdown:
        summary += f"  [{breakdown}]"
    if errors:
        summary += "  -- INCOMPLETE, see query failures below"
    lines.append(summary)

    for bibcode, v in sorted(papers.items(), key=lambda kv: kv[0][:4], reverse=True):
        year = bibcode[:4] if bibcode[:4].isdigit() else "----"
        tags = "".join(t for t in _TAG_ORDER if t in v["sources"])
        title = v["title"] or "(title unavailable)"
        author = v["author"] or "(author unavailable)"
        lines.append(f"    {year}  [{tags:<4}]  {bibcode}  {title}  {author}")

    if errors:
        lines.append("")
        lines.append("  !! QUERY FAILURES -- this position was not fully searched:")
        for err in errors:
            lines.append(f"       {err}")

    return "\n".join(lines)


def parse_coord_list(radec_arg):
    """Return a list of (ra, dec) tuples from either a literal 'ra,dec' arg or a file path."""
    if os.path.isfile(radec_arg):
        coords = []
        with open(radec_arg) as f:
            for lineno, line in enumerate(f, 1):
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                parts = [p.strip() for p in line.split(",")]
                if len(parts) != 2:
                    raise ValueError(f"{radec_arg}:{lineno}: expected 'ra,dec', got {line!r}")
                coords.append((float(parts[0]), float(parts[1])))
        if not coords:
            raise ValueError(f"No coordinate pairs found in {radec_arg}")
        return coords

    parts = [p.strip() for p in radec_arg.split(",")]
    if len(parts) != 2:
        raise ValueError(f"--radec must be 'ra,dec' or a path to a file, got {radec_arg!r}")
    return [(float(parts[0]), float(parts[1]))]


def main():
    parser = argparse.ArgumentParser(
        description="Check the astrophysics literature for sky position(s)."
    )
    parser.add_argument(
        "--radec",
        required=True,
        help="Either 'ra,dec' in degrees, or a path to a text file with one 'ra,dec' pair per line.",
    )
    parser.add_argument(
        "--radius", type=float, default=2.0, help="Search radius in arcsec (default: 2)."
    )
    parser.add_argument(
        "--mission",
        default="JWST",
        help="MAST obs_collection to check for archival coverage (e.g. JWST, HST, TESS). "
        "Pass an empty string to skip the coverage check. (default: JWST)",
    )
    parser.add_argument(
        "--vizier",
        action="store_true",
        help="Also search all of VizieR. Finds papers SIMBAD/NED never catalogued, "
        "but adds roughly a minute per position.",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=8,
        help="Threads used for NED reference and VizieR metadata lookups (default: 8).",
    )
    parser.add_argument(
        "--no-progress",
        action="store_true",
        help="Suppress progress bars (they are written to stderr, and are off by "
        "default when stderr is not a terminal).",
    )
    parser.add_argument(
        "--savefile",
        default=None,
        help="Path to save output. By default output is only printed, not saved.",
    )
    args = parser.parse_args()

    coords = parse_coord_list(args.radec)
    show_progress = not args.no_progress and sys.stderr.isatty()

    reports = []
    for i, (ra, dec) in enumerate(coords, 1):
        if show_progress and len(coords) > 1:
            print(f"[{i}/{len(coords)}] RA={ra:.6f} Dec={dec:.6f}", file=sys.stderr, flush=True)
        reports.append(
            check_object_in_literature(
                ra,
                dec,
                args.radius,
                args.mission,
                use_vizier=args.vizier,
                workers=args.workers,
                show_progress=show_progress,
            )
        )
    output = ("\n\n" + "-" * 40 + "\n\n").join(reports)

    print(output)

    if args.savefile:
        savefile = os.path.expanduser(args.savefile)
        os.makedirs(os.path.dirname(savefile) or ".", exist_ok=True)
        with open(savefile, "w") as f:
            f.write(output + "\n")

        print(f"\nSaved to: {savefile}")


if __name__ == "__main__":
    main()
