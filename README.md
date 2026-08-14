# bibsearch

Check whether one or more sky positions have already appeared in the astrophysics
literature.

Given RA/Dec, `bibsearch` cone-searches **SIMBAD** and **NED** for catalogued objects
at that position and returns the bibliography attached to each match. Optionally it
first confirms archival coverage through **MAST** for a given mission, and — if an ADS
token is available — fills in missing paper titles and authors from **ADS**.

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
| `--savefile` | *(none)* | Write the report to this path as well as stdout |

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

Broader recall generally needs a VizieR cone search (CDS ingests published tables far
more comprehensively than they become SIMBAD objects), an ADS full-text search on the
coordinate-derived source name, and TNS for transients.

## Requirements

Python ≥ 3.9, `astropy`, `astroquery` ≥ 0.4.10. The `ads` package is optional.

## License

MIT
