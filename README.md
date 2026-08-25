# bibsearch

Check whether one or more sky positions have already appeared in the astrophysics
literature. Give it a single RA/Dec or a file of coordinates and it searches **SIMBAD**,
**NED** and **VizieR** for anything published there, returning the combined bibliography
with every bibcode tagged by the service that found it — `[S]`IMBAD, `[N]`ED,
`[V]`izieR, `[A]`DS.

SIMBAD and NED are curated object databases, so they lag and miss things; VizieR indexes
published tables directly and reaches papers the other two never catalogued. All three
run by default, and `--simbad`, `--ned` or `--vizier` restricts the search to exactly
those. It can also check archival coverage via **MAST**, and fill in missing titles and
authors from **ADS** when a token is set.

**A hit is strong evidence; a miss is weak evidence.** These are curated databases, not
a complete index of the literature, so never read an empty result as proof that a source
is unpublished.


```
RA=189.106043  Dec=62.242045  (radius=0.5", services: SIMBAD+NED+VizieR+ADS)
  JWST observations: 203  (target: GN-z11, GNZ11, GNZ7Q, GNz11, ...)
  Resolved name(s): GN-z11, GNZ11, GNz11, ...
  Papers found: 331  [SIMBAD: 285, NED: 109, VizieR: 20]
    ...
    2016  [SN  ]  2016ApJ...819..129O  A Remarkably Luminous Galaxy at z=11.1 Measured with Hubble Space Telescope Grism Spectroscopy  Oesch, P. A.
    ...
```

## Installation

Install into whichever conda environment or virtualenv you want to run it from:

```bash
git clone https://github.com/vadimrusakov/bibsearch
cd bibsearch
pip install ".[ads]"
```

Use the bare `pip install .` only if you have no ADS token — see below for why it
matters.

For an editable install while developing:

```bash
pip install -e .
```

### Optional: ADS enrichment

```bash
pip install ".[ads]"
export ADS_DEV_KEY="your-ads-api-token"   # https://ui.adsabs.harvard.edu/user/settings/token
```

ADS does not extend catalogue coverage — its `object:` search is itself built on
SIMBAD/NED name resolution — but it is what supplies **titles and authors for bibcodes
that only SIMBAD found**. SIMBAD's `biblio` field returns bare bibcodes, and SIMBAD is
usually the largest contributor, so without ADS much of the report reads
`(title unavailable)  (author unavailable)`. NED and VizieR supply their own metadata
and are unaffected.

ADS needs both the `ads` package and `ADS_DEV_KEY`. If either is missing it is dropped
from the search, and `--ads` warns.

## Usage

```bash
# Single position (RA, Dec in degrees)
bibsearch --radec 189.106043,62.242045

# Larger search radius, save the report
bibsearch --radec 189.106043,62.242045 --radius 3 --savefile out.txt

# Check HST archival coverage instead of the JWST default
bibsearch --radec 189.106043,62.242045 --mission HST

# Skip the archive-coverage check entirely
bibsearch --radec 189.106043,62.242045 --mission ""

# Restrict to specific services: skip the slow VizieR pass
bibsearch --radec 189.106043,62.242045 --simbad --ned

# VizieR only
bibsearch --radec 189.106043,62.242045 --vizier

# Many positions from a file
bibsearch --radec coords.txt

# --savefile out.txt also writes out.json: raw VizieR match data (redshift,
# separation, full columns for photometry)
bibsearch --radec 189.106043,62.242045 --savefile out.txt
```

`coords.txt` takes one comma-separated `RA,Dec` pair per line, in degrees.
Blank lines and lines starting with `#` are ignored:

```
# GOODS-N
189.106043,62.242045
189.106043,62.242045
```

### Command line arguments

| Option | Default | Meaning |
| --- | --- | --- |
| `--radec` | *(required)* | `ra,dec` in degrees, or a path to a file of pairs |
| `--radius` | `2.0` | Cone-search radius in arcsec |
| `--mission` | `JWST` | MAST `obs_collection` to check; `""` disables |
| `--simbad` `--ned` `--vizier` | all on | Restrict to the named services (see below) |
| `--workers` | `8` | Threads for NED reference and VizieR metadata lookups |
| `--no-progress` | off | Suppress progress bars |
| `--savefile` | *(none)* | Write the report to this path as well as stdout, and raw per-match VizieR data (see below) to the same path with `.json` in place of its extension |

Some VizieR archive catalogues (`B/eso`, `B/hst`) return free text such as
`"European Southern Observatory (2016)"` in place of a bibcode; these are observation logs rather than papers and are filtered out.

### Raw match data

When VizieR is one of the selected services, every VizieR-sourced paper in the
report gets an extra indented line per matched *source*, showing the
separation and a best-effort redshift straight from that catalogue's own row
-- the same data VizieR indexed to find the position in the first place:

```
    2024  [V   ]  2024A&A...691A.240M  ASTRODEEP-JWST photometry and redshifts  Merlin E.
          [J/A+A/691/A240/catalog]  sep=0.14"  z=10.97 (zphot)  (86 columns -- see --savefile for photometry)
```

If a catalogue has more than one source within the search radius (a crowded
field, a blend), all of them are shown -- nearest first -- not just the
closest, and each line is tagged `N/M`:

```
    2013  [V   ]  2013ApJ...779...25X  Fake Title  Author, A.
          [J/ApJ/779/25/table1 1/2]  sep=0.50"  z=9.234 (zphot)  (5 columns -- see --savefile for photometry)
          [J/ApJ/779/25/table1 2/2]  sep=1.90"  z=3.1 (zphot)  (5 columns -- see --savefile for photometry)
```

Up to `VIZIER_MATCH_ROW_LIMIT` (50) sources per table are captured this way;
none are silently dropped just for having a farther neighbour returned first.

This is skipped by SIMBAD and NED: they resolve to an *object*, with at most
one position/redshift for that object, not a row from each paper's own table,
so there is nothing paper-specific to extract from them.

The redshift printed inline is a best-effort guess (see below), and the row's
other columns -- photometry included -- aren't dumped inline since a table can
have anywhere from a handful to 100+ columns. Pass `--savefile` to get every
column, with units, as JSON alongside the text report (e.g. `--savefile
out.txt` also writes `out.json`).

The JSON file is a list, one entry per input position:

```json
[
  {
    "ra_deg": 189.106043,
    "dec_deg": 62.242045,
    "radius_arcsec": 0.5,
    "matches": [
      {
        "bibcode": "2024A&A...691A.240M",
        "title": "ASTRODEEP-JWST photometry and redshifts",
        "author": "Merlin E.",
        "vizier_table": "J/A+A/691/A240/catalog",
        "parent_catalog": "J/A+A/691/A240",
        "row_index": 0,
        "n_matches_in_table": 1,
        "separation_arcsec": 0.14,
        "redshift": 10.97,
        "redshift_column": "zphot",
        "columns": { "...": "every raw column from the matched row" },
        "units": { "Jmag": "mag", "...": "..." }
      }
    ]
  }
]
```

`row_index`/`n_matches_in_table` distinguish multiple sources from the same
catalogue table within the search radius: `row_index` is 0-based, ordered by
increasing separation, and `n_matches_in_table` is the total count for that
table at this position (2 for the pair above).

`redshift`/`redshift_column` are a best-effort guess over a priority list of
common column names (`redshift`, `zspec`, `zphot`, `zbest`, `z`, ...) --
VizieR naming is not standardised, so always check `columns` for the actual
value if the guess looks wrong or comes back `null`. `columns` holds every
raw column from the matched row (photometry included, whatever that catalogue
publishes) with `units` giving the unit for any column that has one, so
photometry can be pulled out programmatically without re-querying VizieR.
`separation_arcsec` comes from VizieR's own distance-from-center column,
falling back to a manual RA/Dec computation if a catalogue doesn't provide it.

### Failed queries are reported, never silent

If any service errors, the affected position is flagged and the failures are listed:

```
  Papers found: 11  [SIMBAD: 11]  -- INCOMPLETE, see query failures below
  ...
  !! QUERY FAILURES -- this position was not fully searched:
       VizieR cone search: ConnectionError: VizieR TAP timed out
       NED references for UDF:[CBS2006] 02858: TimeoutError: read timed out
```

## Requirements

Python ≥ 3.9, `astropy`, `astroquery` ≥ 0.4.10. `ads` and `tqdm` are optional.

## License

MIT
