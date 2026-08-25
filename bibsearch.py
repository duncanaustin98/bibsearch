#!/usr/bin/env python3
"""
Check whether sky position(s) have appeared in the astrophysics literature.

Workflow: optionally confirm archival coverage via MAST for a given mission,
then pull the bibliography for that position from SIMBAD (biblio field), NED
(references table) and VizieR (catalogue metadata). SIMBAD and NED are
object-centric and curated, so they lag and miss things; VizieR indexes
published tables directly and reaches papers the other two never catalogued.
ADS is a supplement/title-enrichment layer that needs a key -- searching ADS by
object name alone is unreliable, so it is not a primary source.

All four run by default. Passing any of --simbad/--ned/--vizier/--ads restricts
the search to exactly those services.

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
    bibsearch --radec 53.15398,-27.80095 --simbad --ned    # skip the slow VizieR pass
    bibsearch --radec 53.15398,-27.80095 --vizier          # VizieR only
    bibsearch --radec 53.15398,-27.80095 --savefile out.txt  # also writes out.json: raw VizieR rows (z, sep, photometry)

coords.txt format (two comma-separated columns, RA,Dec in degrees, one pair per line):
    53.15398,-27.80095
    53.16,-27.81
"""

import argparse
import html
import json
import math
import os
import pickle
import random
import re
import sys
import threading
import time
import warnings
from concurrent.futures import ThreadPoolExecutor, as_completed
from xml.parsers.expat import ExpatError

import numpy as np
from astropy.utils.exceptions import AstropyWarning
from astroquery.exceptions import NoResultsWarning, TableParseError

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

NED_CACHE_LOCATION = "/nvme/scratch/work/austind/.astroquery_cache/Ned"
VIZIER_CACHE_LOCATION = "/nvme/scratch/work/austind/.astroquery_cache/Vizier"

# Ned is used as a persistent singleton everywhere below, so setting these once
# here is enough. Vizier is not: vizier_lookup() calls Vizier(...), which builds
# a *new* VizierClass instance via __call__ each time, resetting cache_location
# (and TIMEOUT) to their class defaults -- ~/.astropy/cache, on a filesystem
# where the home quota is often exhausted. So VIZIER_CACHE_LOCATION is applied
# to each fresh instance explicitly in vizier_lookup() instead of here.
Ned.cache_location = NED_CACHE_LOCATION
Ned.TIMEOUT = 180

Simbad.add_votable_fields("biblio")

# Region queries hit external services that occasionally time out or drop the
# connection transiently; retry a couple of times before giving up.
RETRY_ATTEMPTS = 3
RETRY_BACKOFF_SECS = 5
# +/-25% jitter on every backoff sleep. NED reference-table fetches run
# `workers` (default 8) requests concurrently, all starting their retry
# countdown at roughly the same moment on a shared failure (e.g. NED
# rate-limiting the burst) -- with a fixed backoff every thread retries in
# lockstep and re-creates the exact same burst that got them rate-limited in
# the first place. Jitter spreads the retries out instead.
RETRY_JITTER_FRAC = 0.25

# astroquery caches every HTTP response to disk (keyed by request hash) before
# checking its status code, and its cache reader doesn't guard against a
# truncated file. So a transient 503 or a run killed mid-write leaves a
# corrupt cache entry -- either empty (pickle.load raises EOFError) or a
# cached HTML error page that the caller then tries to parse as XML/VOTable
# (ExpatError). Once that happens, every subsequent call for that exact query
# reads the same bad entry back and fails identically forever: retrying the
# call alone never helps. clear_cache, when given, is invoked on exactly
# these two error types so the retry actually re-fetches from the network.
#
# Vizier wraps whatever blew up while parsing the VOTable body (ExpatError
# included) in its own TableParseError before raising, rather than letting
# the original exception propagate -- see _is_cache_corruption below, which
# unwraps it via __context__ (Python sets this automatically for a bare
# `raise NewError(...)` inside an `except ... as ex:` block, even without
# `raise ... from ex`) so this case is still recognised as corruption.
CACHE_CORRUPTION_ERRORS = (EOFError, ExpatError, pickle.UnpicklingError)

_cache_clear_lock = threading.Lock()


def _is_cache_corruption(e):
    """True if `e` looks like on-disk astroquery cache corruption (see
    CACHE_CORRUPTION_ERRORS), including when it arrives wrapped in a VizieR
    TableParseError rather than raised directly."""
    if isinstance(e, CACHE_CORRUPTION_ERRORS):
        return True
    if isinstance(e, TableParseError):
        return isinstance(e.__context__, CACHE_CORRUPTION_ERRORS)
    return False


def _clear_cache_safely(clear_fn):
    """Run a Service.clear_cache under a lock so concurrent callers (this is
    invoked from thread pools) don't race to unlink() the same files."""
    with _cache_clear_lock:
        try:
            clear_fn()
        except FileNotFoundError:
            pass


def _retry(fn, attempts=RETRY_ATTEMPTS, backoff=RETRY_BACKOFF_SECS, clear_cache=None, no_retry=()):
    """Call a zero-arg callable, retrying on exception with linear backoff.

    `no_retry` is a tuple of exception types that should propagate
    immediately without consuming an attempt (e.g. a service's "no data for
    this object" signal, which is a normal outcome, not a fault worth
    retrying). `clear_cache` is called before backing off whenever the
    exception looks like on-disk cache corruption (see
    CACHE_CORRUPTION_ERRORS above).
    """
    for attempt in range(attempts):
        try:
            return fn()
        except no_retry:
            raise
        except Exception as e:
            if attempt == attempts - 1:
                raise
            if clear_cache is not None and _is_cache_corruption(e):
                _clear_cache_safely(clear_cache)
            delay = backoff * (attempt + 1)
            delay *= 1 + random.uniform(-RETRY_JITTER_FRAC, RETRY_JITTER_FRAC)
            time.sleep(delay)

# A bibcode is exactly 19 characters and contains no whitespace. VizieR's
# origin_article field returns free text for some archive catalogues
# ("European Southern Observatory (2016)"), which this filters out.
BIBCODE_RE = re.compile(r"^\d{4}\S{15}$")

# Candidate RA/Dec column pairs used as a fallback separation calculation when
# a VizieR table lacks the "_r" distance column (only added when explicitly
# requested via columns=["*", "+_r"], and not every catalogue honours it).
RADEC_COLUMN_CANDIDATES = (
    ("_RAJ2000", "_DEJ2000"),
    ("RAJ2000", "DEJ2000"),
    ("RA_ICRS", "DE_ICRS"),
    ("RAdeg", "DEdeg"),
    ("RA", "DEC"),
    ("ra", "dec"),
)

# Cap on rows returned per VizieR table for a single cone search. Sorted by
# separation ascending (see the "+_r" columns request in vizier_lookup), so
# this is "closest N sources in this catalogue", not an arbitrary truncation.
# Search radii here are a few arcsec at most, so 50 is generous headroom for
# a crowded field while still bounding a pathologically large --radius.
VIZIER_MATCH_ROW_LIMIT = 50

# Column names checked, in priority order, when guessing which column holds a
# redshift. This is a heuristic over wildly inconsistent VizieR naming
# conventions, not a guarantee -- the full raw row is always kept too so any
# missed redshift (or photometry) column can be recovered by hand.
REDSHIFT_COLUMN_PRIORITY = (
    "redshift", "zspec", "zphot", "zbest",
    "z_spec", "z_phot", "zsp", "zph", "z",
)


def _to_native(val):
    """Convert one astropy/numpy table cell to a JSON-serialisable Python value."""
    if val is np.ma.masked:
        return None
    if isinstance(val, (bytes, np.bytes_)):
        return val.decode("utf-8", "replace")
    if isinstance(val, np.generic):
        val = val.item()
    if isinstance(val, float) and math.isnan(val):
        return None
    return val


def _parent_catalog(name):
    """Collapse a VizieR sub-table name to its parent catalogue (shared bibcode)."""
    return "/".join(name.split("/")[:-1]) if name.count("/") > 1 else name


def _extract_separation_arcsec(row, table, coord):
    """Return the on-sky separation (arcsec) between `coord` and this matched row."""
    if "_r" in table.colnames:
        native = _to_native(row["_r"])
        if native is not None:
            # VizieR's ASU convention is arcmin, but the CDS service actually
            # returns "_r" in arcsec without populating column.unit (checked
            # empirically: with radius=1", every returned "_r" was <= 1 only
            # when read as arcsec -- reading as arcmin put values 60x over
            # the search radius). Trust the metadata if it's ever populated;
            # arcsec is the correct fallback, not arcmin.
            unit = table["_r"].unit or u.arcsec
            try:
                return float((native * unit).to(u.arcsec).value)
            except Exception:
                pass
    for ra_col, dec_col in RADEC_COLUMN_CANDIDATES:
        if ra_col not in table.colnames or dec_col not in table.colnames:
            continue
        ra_val = _to_native(row[ra_col])
        dec_val = _to_native(row[dec_col])
        if ra_val is None or dec_val is None:
            continue
        try:
            ra_unit = table[ra_col].unit or u.deg
            dec_unit = table[dec_col].unit or u.deg
            match_coord = SkyCoord(ra=ra_val * ra_unit, dec=dec_val * dec_unit, frame="icrs")
            return float(coord.separation(match_coord).arcsec)
        except Exception:
            continue
    return None


def _extract_redshift(row, table):
    """Return (value, column_name) for the first column matching a known redshift name."""
    lower_map = {}
    for c in table.colnames:
        lower_map.setdefault(c.lower(), c)
    for candidate in REDSHIFT_COLUMN_PRIORITY:
        col = lower_map.get(candidate)
        if col is None:
            continue
        val = _to_native(row[col])
        if val is not None:
            return val, col
    return None, None

# Order in which provenance tags are displayed.
_TAG_ORDER = "SNVA"

# Bibliography services, in display order. All are used unless the caller
# selects a subset on the command line.
SERVICE_ORDER = ("simbad", "ned", "vizier", "ads")
SERVICE_LABELS = {"simbad": "SIMBAD", "ned": "NED", "vizier": "VizieR", "ads": "ADS"}
DEFAULT_SERVICES = frozenset(SERVICE_ORDER)


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
        result = _retry(
            lambda: Simbad.query_region(coord, radius=radius_arcsec * u.arcsec),
            clear_cache=Simbad.clear_cache,
        )
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
        result = _retry(
            lambda: Ned.query_region(coord, radius=radius_arcsec * u.arcsec),
            clear_cache=Ned.clear_cache,
        )
    except Exception as e:
        return {}, [f"NED region query: {_fmt_exc(e)}"]
    if result is None:
        return {}, []

    names = [str(row["Object Name"]) for row in result]
    hits = {}
    errors = []

    def fetch(name):
        return _retry(
            lambda: Ned.get_table(name, table="references"),
            clear_cache=Ned.clear_cache,
            no_retry=(RemoteServiceError,),
        )

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
    """Return ({bibcode: (title, first_author)}, raw_matches, errors) for VizieR
    catalogues covering the position.

    VizieR indexes published tables directly, so it reaches papers whose sources
    were never folded into SIMBAD or NED as catalogued objects. Sub-tables are
    collapsed to their parent catalogue before metadata is fetched.

    `raw_matches` is a list of one record per matched *source* -- separation,
    a best-effort redshift, and the full raw row (all columns, with units) --
    for every source, in every sub-table, whose parent catalogue resolved to a
    valid bibcode. A catalogue can have more than one source within the search
    radius (crowded fields, blends); all of them are returned, sorted nearest
    first, up to VIZIER_MATCH_ROW_LIMIT per table -- none are silently dropped
    for having a farther neighbour take the "first row". This is the actual
    catalogue data (redshifts, photometry, ...) behind each "successfully
    cross-matched" paper, not just its metadata.
    """
    def new_vizier(**kwargs):
        # Vizier(...) builds a fresh VizierClass instance each call (see the
        # cache_location comment near the top of this file), so cache_location
        # must be set on every instance, not just once at import time.
        v = Vizier(**kwargs)
        v.cache_location = VIZIER_CACHE_LOCATION
        return v

    try:
        # "+_r" (not just "_r") requests both the separation column *and*
        # sorting by it ascending -- a leading +/- on a VizieR column name is
        # a sort request, so row N is always the N-th closest source.
        result = _retry(
            lambda: new_vizier(row_limit=VIZIER_MATCH_ROW_LIMIT, columns=["*", "+_r"]).query_region(
                coord, radius=radius_arcsec * u.arcsec
            ),
            clear_cache=lambda: new_vizier().clear_cache(),
        )
    except Exception as e:
        return {}, [], [f"VizieR cone search: {_fmt_exc(e)}"]

    tables_by_name = {t.meta.get("name"): t for t in result if t.meta.get("name")}
    parents = sorted({_parent_catalog(name) for name in tables_by_name})
    if not parents:
        return {}, [], []

    papers = {}
    catalog_bibcode = {}  # parent catalog -> bibcode, for joining raw rows below
    errors = []

    def fetch(catalog):
        return _retry(
            lambda: new_vizier().get_catalog_metadata(catalog=catalog),
            clear_cache=lambda: new_vizier().clear_cache(),
        )

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
            title, author = title or None, authors.split(";")[0].strip() or None
            papers[bibcode] = (title, author)
            catalog_bibcode[catalog] = bibcode

    raw_matches = []
    for name, table in tables_by_name.items():
        if len(table) == 0:
            continue
        bibcode = catalog_bibcode.get(_parent_catalog(name))
        if not bibcode:
            continue
        title, author = papers[bibcode]
        units = {c: str(table[c].unit) for c in table.colnames if table[c].unit is not None}
        for row_index, row in enumerate(table):
            separation_arcsec = _extract_separation_arcsec(row, table, coord)
            redshift, redshift_column = _extract_redshift(row, table)
            raw_matches.append({
                "bibcode": bibcode,
                "title": title,
                "author": author,
                "vizier_table": name,
                "parent_catalog": _parent_catalog(name),
                "row_index": row_index,
                "n_matches_in_table": len(table),
                "separation_arcsec": separation_arcsec,
                "redshift": redshift,
                "redshift_column": redshift_column,
                "columns": {c: _to_native(row[c]) for c in table.colnames},
                "units": units,
            })

    return papers, raw_matches, errors


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
    services=None,
    workers=8,
    show_progress=False,
):
    """Return (report_string, vizier_raw_matches) for one RA/Dec pair.

    `services` is the set of bibliography services to query; it defaults to all
    of them (see DEFAULT_SERVICES). `vizier_raw_matches` is the raw per-row
    VizieR data described in `vizier_lookup` -- empty unless "vizier" is
    selected.
    """
    services = DEFAULT_SERVICES if services is None else frozenset(services)
    used = "+".join(SERVICE_LABELS[s] for s in SERVICE_ORDER if s in services) or "none"
    lines = [f"RA={ra_deg:.6f}  Dec={dec_deg:.6f}  (radius={radius_arcsec}\", services: {used})"]

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

    simbad_hits = {}
    if "simbad" in services:
        simbad_hits, simbad_errors = simbad_lookup(coord, radius_arcsec)
        errors += simbad_errors

    ned_hits = {}
    if "ned" in services:
        ned_hits, ned_errors = ned_lookup(coord, radius_arcsec, workers, show_progress)
        errors += ned_errors

    vizier_papers = {}
    vizier_raw = []
    if "vizier" in services:
        vizier_papers, vizier_raw, vizier_errors = vizier_lookup(coord, radius_arcsec, workers, show_progress)
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

    if "ads" in services:
        # ADS is searched by resolved object name, so it can only contribute when
        # SIMBAD or NED supplied names. Say so rather than returning a silent zero.
        if not (services & {"simbad", "ned"}):
            lines.append(
                "  Note: ADS is searched by object name, which only SIMBAD and NED "
                "resolve -- select one of them for ADS to contribute."
            )
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

    raw_by_bibcode = {}
    for entry in vizier_raw:
        raw_by_bibcode.setdefault(entry["bibcode"], []).append(entry)

    for bibcode, v in sorted(papers.items(), key=lambda kv: kv[0][:4], reverse=True):
        year = bibcode[:4] if bibcode[:4].isdigit() else "----"
        tags = "".join(t for t in _TAG_ORDER if t in v["sources"])
        title = v["title"] or "(title unavailable)"
        author = v["author"] or "(author unavailable)"
        lines.append(f"    {year}  [{tags:<4}]  {bibcode}  {title}  {author}")
        for entry in raw_by_bibcode.get(bibcode, []):
            sep = entry["separation_arcsec"]
            sep_str = f'{sep:.2f}"' if sep is not None else "n/a"
            if entry["redshift"] is not None:
                z_str = f'{entry["redshift"]} ({entry["redshift_column"]})'
            else:
                z_str = "n/a"
            n_cols = len(entry["columns"])
            table_tag = f"{entry['vizier_table']}"
            if entry["n_matches_in_table"] > 1:
                table_tag += f" {entry['row_index'] + 1}/{entry['n_matches_in_table']}"
            lines.append(
                f"          [{table_tag}]  sep={sep_str}  z={z_str}"
                f"  ({n_cols} column{'s' if n_cols != 1 else ''} -- see --savefile for photometry)"
            )

    if errors:
        lines.append("")
        lines.append("  !! QUERY FAILURES -- this position was not fully searched:")
        for err in errors:
            lines.append(f"       {err}")

    return "\n".join(lines), vizier_raw


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
    services = parser.add_argument_group(
        "service selection",
        "By default SIMBAD, NED, VizieR and ADS are all used. Passing any of these "
        "flags restricts the search to exactly the ones given.",
    )
    services.add_argument(
        "--simbad", action="store_true", help="Query SIMBAD (object bibliographies)."
    )
    services.add_argument(
        "--ned", action="store_true", help="Query NED (extragalactic object references)."
    )
    services.add_argument(
        "--vizier",
        action="store_true",
        help="Query VizieR (published catalogue tables). Much the best recall, but "
        "the slowest step -- roughly 20-60s per position.",
    )
    services.add_argument(
        "--ads",
        action="store_true",
        help="Query ADS by resolved object name and fill in missing titles/authors. "
        "Needs ADS_DEV_KEY and depends on SIMBAD or NED for name resolution.",
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
        help="Path to save output. By default output is only printed, not saved. "
        "Also writes the raw per-match VizieR data (bibcode, separation, "
        "best-effort redshift, and every raw column with units, so photometry "
        "can be extracted later) as JSON alongside it, at the same path with "
        "its extension replaced by .json.",
    )
    args = parser.parse_args()

    coords = parse_coord_list(args.radec)
    show_progress = not args.no_progress and sys.stderr.isatty()

    selected = frozenset(s for s in SERVICE_ORDER if getattr(args, s))
    explicit = bool(selected)
    if not selected:
        selected = DEFAULT_SERVICES

    # ADS needs both the package and a token. Drop it when either is missing so the
    # report never claims a service it could not use, and say why if it was asked for.
    if "ads" in selected:
        if ads is None:
            reason = 'the "ads" package is not installed (pip install ".[ads]")'
        elif not os.environ.get("ADS_DEV_KEY"):
            reason = "ADS_DEV_KEY is not set"
        else:
            reason = None
        if reason:
            selected -= {"ads"}
            if explicit:
                print(f"warning: ADS unavailable -- {reason}.", file=sys.stderr)
                print(
                    "         Bibcodes found only by SIMBAD will have no title or author.",
                    file=sys.stderr,
                )

    if args.savefile and "vizier" not in selected:
        print(
            "warning: VizieR is not among the selected services, so the raw "
            "match data JSON will be written with empty 'matches' lists.",
            file=sys.stderr,
        )

    reports = []
    raw_positions = []
    for i, (ra, dec) in enumerate(coords, 1):
        if show_progress and len(coords) > 1:
            print(f"[{i}/{len(coords)}] RA={ra:.6f} Dec={dec:.6f}", file=sys.stderr, flush=True)
        report, vizier_raw = check_object_in_literature(
            ra,
            dec,
            args.radius,
            args.mission,
            services=selected,
            workers=args.workers,
            show_progress=show_progress,
        )
        reports.append(report)
        raw_positions.append({
            "ra_deg": ra,
            "dec_deg": dec,
            "radius_arcsec": args.radius,
            "matches": vizier_raw,
        })
    output = ("\n\n" + "-" * 40 + "\n\n").join(reports)

    print(output)

    if args.savefile:
        savefile = os.path.expanduser(args.savefile)
        os.makedirs(os.path.dirname(savefile) or ".", exist_ok=True)
        with open(savefile, "w") as f:
            f.write(output + "\n")

        print(f"\nSaved to: {savefile}")

        rawfile = os.path.splitext(savefile)[0] + ".json"
        with open(rawfile, "w") as f:
            json.dump(raw_positions, f, indent=2)

        n_rows = sum(len(p["matches"]) for p in raw_positions)
        print(f"Raw VizieR data saved to: {rawfile} ({n_rows} row(s) across {len(raw_positions)} position(s))")


if __name__ == "__main__":
    main()
