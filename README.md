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
git clone https://github.com/<your-username>/bibsearch.git
cd bibsearch
pip install .
```

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
| `--savefile` | *(none)* | Write the report to this path as well as stdout |


Some VizieR archive catalogues (`B/eso`, `B/hst`) return free text such as
`"European Southern Observatory (2016)"` in place of a bibcode; these are observation logs rather than papers and are filtered out.

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
