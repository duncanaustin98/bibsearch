#!/usr/bin/env python3
"""
Check whether sky position(s) have appeared in the astrophysics literature.

Workflow: optionally confirm archival coverage via MAST for a given mission,
then pull the bibliography directly from SIMBAD (biblio field) and NED
(references table) for any catalogued object at that position. ADS is used
only as an optional supplement/title-enrichment layer when a key is set --
searching ADS by object name alone is unreliable, so it is not the primary
source.

Setup:
    pip install astroquery ads
    export ADS_DEV_KEY="your-ads-api-token"   # https://ui.adsabs.harvard.edu/user/settings/token (optional)

Usage:
    bibsearch --radec 53.15398,-27.80095
    bibsearch --radec coords.txt
    bibsearch --radec 53.15398,-27.80095 --radius 3 --savefile out.txt
    bibsearch --radec 53.15398,-27.80095 --mission HST

coords.txt format (two comma-separated columns, RA,Dec in degrees, one pair per line):
    53.15398,-27.80095
    53.16,-27.81
"""

import argparse
import html
import os
import warnings

from astropy.utils.exceptions import AstropyWarning
from astroquery.exceptions import NoResultsWarning

warnings.simplefilter("ignore", category=NoResultsWarning)
warnings.simplefilter("ignore", category=AstropyWarning)

import astropy.units as u
from astropy.coordinates import SkyCoord
from astroquery.mast import Observations
from astroquery.simbad import Simbad
from astroquery.ipac.ned import Ned

try:
    import ads
except ImportError:
    # Optional: only used to enrich titles/authors, and only when ADS_DEV_KEY is set.
    ads = None

Simbad.add_votable_fields("biblio")


def check_mission_coverage(coord, radius_arcsec, mission):
    return Observations.query_criteria(
        coordinates=coord,
        radius=radius_arcsec * u.arcsec,
        obs_collection=mission,
    )


def simbad_lookup(coord, radius_arcsec):
    """Return {main_id: set(bibcodes)} pulled directly from SIMBAD's biblio field."""
    try:
        result = Simbad.query_region(coord, radius=radius_arcsec * u.arcsec)
    except Exception:
        return {}
    if result is None:
        return {}

    hits = {}
    for row in result:
        name = str(row["main_id"])
        biblio = row["biblio"]
        bibcodes = set(str(biblio).split("|")) if biblio and str(biblio) not in ("", "--") else set()
        hits[name] = bibcodes
    return hits


def ned_lookup(coord, radius_arcsec):
    """Return {name: {bibcode: (title, first_author)}} pulled directly from NED's references table."""
    try:
        result = Ned.query_region(coord, radius=radius_arcsec * u.arcsec)
    except Exception:
        return {}
    if result is None:
        return {}

    hits = {}
    for row in result:
        name = str(row["Object Name"])
        try:
            refs = Ned.get_table(name, table="references")
        except Exception:
            refs = None
        papers = {}
        if refs is not None:
            for r in refs:
                title = html.unescape(str(r["Article Title"]))
                author = str(r["Author List"]).split(";")[0].strip()
                papers[str(r["Refcode"])] = (title, author)
        hits[name] = papers
    return hits


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


def check_object_in_literature(ra_deg, dec_deg, radius_arcsec, mission):
    """Return a formatted report string for one RA/Dec pair."""
    lines = [f"RA={ra_deg:.6f}  Dec={dec_deg:.6f}  (radius={radius_arcsec}\")"]

    coord = SkyCoord(ra=ra_deg * u.deg, dec=dec_deg * u.deg, frame="icrs")

    target_names = set()
    if mission:
        try:
            mission_obs = check_mission_coverage(coord, radius_arcsec, mission)
        except Exception as e:
            mission_obs = []
            lines.append(f"  MAST query failed: {e}")

        target_names = set(mission_obs["target_name"]) if len(mission_obs) else set()
        mission_line = f"  {mission} observations: {len(mission_obs)}"
        if target_names:
            mission_line += f"  (target: {', '.join(sorted(target_names))})"
        lines.append(mission_line)

    simbad_hits = simbad_lookup(coord, radius_arcsec)
    ned_hits = ned_lookup(coord, radius_arcsec)
    names = set(simbad_hits) | set(ned_hits) | target_names

    if names:
        lines.append(f"  Resolved name(s): {', '.join(sorted(names))}")
    else:
        lines.append("  Resolved name(s): none (uncatalogued position)")

    papers = {}  # bibcode -> {"title": ..., "author": ...}

    def merge(bibcode, title=None, author=None):
        entry = papers.setdefault(bibcode, {"title": None, "author": None})
        if title and not entry["title"]:
            entry["title"] = title
        if author and not entry["author"]:
            entry["author"] = author

    for bibcodes in simbad_hits.values():
        for bc in bibcodes:
            merge(bc)
    for refs in ned_hits.values():
        for bc, (title, author) in refs.items():
            merge(bc, title=title, author=author)  # NED metadata takes priority (fetched directly, no key needed)

    for name in set(simbad_hits) | set(ned_hits):
        for p in find_papers_for_object(name):
            title = p.title[0] if p.title else None
            author = p.author[0] if p.author else None
            merge(p.bibcode, title=title, author=author)

    missing = [bc for bc, v in papers.items() if not v["title"] or not v["author"]]
    if missing:
        for bc, (title, author) in enrich_metadata(missing).items():
            merge(bc, title=title, author=author)

    lines.append(f"  Papers found: {len(papers)}")
    for bibcode, v in sorted(papers.items(), key=lambda kv: kv[0][:4], reverse=True):
        year = bibcode[:4] if bibcode[:4].isdigit() else "----"
        title = v["title"] or "(title unavailable)"
        author = v["author"] or "(author unavailable)"
        lines.append(f"    {year}  {bibcode}  {title}  {author}")

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
        "--savefile",
        default=None,
        help="Path to save output. By default output is only printed, not saved.",
    )
    args = parser.parse_args()

    coords = parse_coord_list(args.radec)

    reports = [
        check_object_in_literature(ra, dec, args.radius, args.mission) for ra, dec in coords
    ]
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
