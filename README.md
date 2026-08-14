# bibsearch

Check whether one or more sky positions have already appeared in the astrophysics
literature.

Given RA/Dec, `bibsearch` searches **SIMBAD**, **NED** and **VizieR** for anything
published at that position and returns the combined bibliography. SIMBAD and NED are
object-centric and curated, so they lag and miss things; VizieR indexes published
tables directly and reaches papers the other two never catalogued. Optionally it first
confirms archival coverage through **MAST** for a given mission, and — if an ADS token
is available — fills in missing paper titles and authors from **ADS**.

All four services run by default. Passing any of `--simbad`, `--ned`, `--vizier`,
`--ads` restricts the search to exactly those.

Every bibcode is tagged with the service that found it, so you can see which source
contributed what:

```
RA=53.153980  Dec=-27.800950  (radius=2.0", services: SIMBAD+NED+VizieR+ADS)
  Resolved name(s): CANDELS J033236.88-274803.7, UDF  2878, ...
  Papers found: 64  [SIMBAD: 11, NED: 25, VizieR: 52]
    2026  [V   ]  2026A&A...709A.205S  Transition from outside-in to inside-out at z~2  Song J.
    2025  [S   ]  2025A&A...697A.175S  JADES: A large population of obscured, ...  Scholtz, Jan
    2018  [SN  ]  2018ApJ...854...29M  The Number Density Evolution of Extreme ...  Maseda, Michael V.
```

`[S]`IMBAD, `[N]`ED, `[V]`izieR, `[A]`DS.

## Installation

Install into whichever conda environment or virtualenv you want to run it from:

```bash
git clone https://github.com/<your-username>/bibsearch.git
cd bibsearch
pip install .
```

This puts a `bibsearch` launcher in that environment's `bin/`, wired to that
environment's interpreter. Activate the environment and the command is on your `PATH`.

For an editable install while developing:

```bash
pip install -e .
```

### Optional: ADS enrichment

```bash
pip install ".[ads]"
export ADS_DEV_KEY="your-ads-api-token"   # https://ui.adsabs.harvard.edu/user/settings/token
```

ADS is a supplement only — it fills in titles and authors that NED did not supply.
The tool is fully functional without it. Note that ADS's `object:` search is itself
built on SIMBAD/NED name resolution, so it does not extend catalogue coverage.

## Usage

```bash
# Single position (RA, Dec in degrees)
bibsearch --radec 53.15398,-27.80095

# Larger search radius, save the report
bibsearch --radec 53.15398,-27.80095 --radius 3 --savefile out.txt

# Check HST archival coverage instead of the JWST default
bibsearch --radec 53.15398,-27.80095 --mission HST

# Skip the archive-coverage check entirely
bibsearch --radec 53.15398,-27.80095 --mission ""

# Restrict to specific services: skip the slow VizieR pass
bibsearch --radec 53.15398,-27.80095 --simbad --ned

# VizieR only
bibsearch --radec 53.15398,-27.80095 --vizier

# Many positions from a file
bibsearch --radec coords.txt
```

`coords.txt` takes one comma-separated `RA,Dec` pair per line, in degrees.
Blank lines and lines starting with `#` are ignored:

```
# GOODS-S
53.15398,-27.80095
53.16,-27.81
```

### Options

| Option | Default | Meaning |
| --- | --- | --- |
| `--radec` | *(required)* | `ra,dec` in degrees, or a path to a file of pairs |
| `--radius` | `2.0` | Cone-search radius in arcsec |
| `--mission` | `JWST` | MAST `obs_collection` to check; `""` disables |
| `--simbad` `--ned` `--vizier` `--ads` | all on | Restrict to the named services (see below) |
| `--workers` | `8` | Threads for NED reference and VizieR metadata lookups |
| `--no-progress` | off | Suppress progress bars |
| `--savefile` | *(none)* | Write the report to this path as well as stdout |

### Choosing services

With no service flag, all four are used. Any flag you pass becomes the whole set:

| Invocation | Services used |
| --- | --- |
| `bibsearch --radec …` | SIMBAD + NED + VizieR + ADS |
| `bibsearch --radec … --simbad --ned` | SIMBAD + NED — skips the slow VizieR pass |
| `bibsearch --radec … --vizier` | VizieR only |

The header line of each report records which services ran, so saved output stays
self-documenting.

`--ads` is a special case: ADS is searched by resolved object *name*, and only SIMBAD
and NED resolve names. `--ads` on its own therefore finds nothing, and says so rather
than reporting a bare zero.

Progress bars are written to **stderr** and appear only when stderr is a terminal, so
piping or `--savefile` output stays clean.

### The VizieR search

VizieR indexes published tables directly, far more comprehensively than those sources
become SIMBAD or NED objects, so it dominates recall. On a test position in the HUDF it
takes the paper count from 31 to 64 — roughly half the results come from VizieR alone.

It is also the slowest step, costing roughly 20–60s per position: the all-VizieR cone
search is a single slow call, and each matching catalogue then needs a metadata lookup
to recover its bibcode. Those lookups are threaded (`--workers`). If you need a quick
answer, `--simbad --ned` skips it.

Some VizieR archive catalogues (`B/eso`, `B/hst`) return free text such as
`"European Southern Observatory (2016)"` in place of a bibcode; these are observation
logs rather than papers and are filtered out.

### Failed queries are reported, never silent

If any service errors, the affected position is flagged and the failures are listed:

```
  Papers found: 11  [SIMBAD: 11]  -- INCOMPLETE, see query failures below
  ...
  !! QUERY FAILURES -- this position was not fully searched:
       VizieR cone search: ConnectionError: VizieR TAP timed out
       NED references for UDF:[CBS2006] 02858: TimeoutError: read timed out
```

This matters because the tool's main use is trusting a *negative* result. A timeout
that silently returned zero papers would be indistinguishable from a genuine absence.
Note that NED signals "this object has no references" with an error response, which is
treated as a normal empty result rather than a failure.

## Limitations

**A hit is strong evidence; a miss is weak evidence.** SIMBAD and NED are curated
databases, not a complete index of the literature. Do not treat an empty result as
proof that a source is unpublished. In particular:

- **Curation is selective and lags.** Both databases are compiled by human curators
  from a defined journal list. Recent papers can take months to years to be ingested
  and linked to objects, so newly reported sources are exactly the ones most likely to
  be missed. Proceedings, theses and preprints are largely outside the ingest stream.
- **Object linkage is not paper presence.** A paper can be in SIMBAD's bibliographic
  database yet not linked to a given object if no curator tagged it — for instance
  when the source sits in a large table that was never ingested.
- **Whole classes of object are absent.** Solar system bodies, transients without a
  permanent designation, gravitational-wave and neutrino events are covered by other
  services (MPC, TNS, GraceDB). SIMBAD is weighted towards Galactic objects, NED
  towards extragalactic ones.
- **Positional matching is imperfect.** The 2″ default is a compromise. Older
  catalogues carry large astrometric errors (IRAS positions can be off by an
  arcminute), high-proper-motion stars drift away from their catalogued epoch, and
  extended objects are stored as a single centroid. Conversely, in crowded or deep
  fields a match may belong to a neighbouring source rather than yours.

`--vizier` addresses the first of these directly. Beyond it, broader recall would need
an ADS full-text search on the coordinate-derived source name (many sources are named
from their coordinates, e.g. `J033236.9-274803`, and full-text search is independent of
any curation), and TNS for transients.

One remaining gap: the optional ADS calls still fail silently, because "no
`ADS_DEV_KEY` set" is a normal state and would otherwise produce a warning on every
run. ADS only supplements titles and authors, so this does not affect which papers are
found — but unlike the other services, an ADS outage will not be reported.

## Requirements

Python ≥ 3.9, `astropy`, `astroquery` ≥ 0.4.10. `ads` and `tqdm` are optional.

## License

MIT
