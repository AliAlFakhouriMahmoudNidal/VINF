# VINF

Extract the saved RHS HTML using standard-library Python:

```powershell
python -u extract_tsv.py
```

Defaults are `data/html` and `data/plants.tsv`. To use other locations:

```powershell
python -u extract_tsv.py --input data/html --output data/plants.tsv
```

The extractor reads one HTML file at a time and keeps only extracted records in
memory. It uses `re` for field extraction. The standard-library `json` module
only decodes escaped characters in individual strings found by regex.
There are no third-party dependencies. The default uses one thread and process.
Optional `--readers 4` reads small batches of files concurrently while extraction
and merging remain in the main thread. Fresh-file checks on this dataset showed
about 2.5 times higher throughput with four threads; the gain depends on storage.

```powershell
python -u extract_tsv.py --readers 4
```

Use `--limit` to test on the first N files in numeric filename order:

```powershell
python -u extract_tsv.py --readers 4 --limit 10000 --output data/plants-test.tsv
```

Without `--limit`, all files are processed. The limit counts input files, including
failed files; canonical merging can produce fewer output rows. Progress and ETA
use the selected number of files. A limit larger than the available files simply
processes all available files.

The TSV has these columns, in order:

```text
canonical_id, source_url, primary_name, alternative_names, genus, family,
description, plant_range, plant_types, foliage, habit, soil_types, moisture,
exposure, position
```

Each canonical plant gets one row. The canonical page supplies preferred values;
related records contribute alternative names and fill missing values. If its
canonical page was not parsed, the first valid related record supplies the name
and a warning is logged. Empty descriptions stay empty.

`alternative_names`, `plant_types`, `foliage`, `habit`, `soil_types`, `moisture`,
`exposure`, and `position` are lists separated by `|`. For example:
`Moist but well-drained|Well-drained`. Literal `\` and `|` inside list items are
escaped as `\\` and `\|`. An empty cell represents a missing value or empty list.
Text keeps its case, and tabs/newlines inside values become spaces.
The file is UTF-8 without a BOM, with LF line endings and no CSV-style quoting.

For a future importer, list cells can be decoded with standard-library Python:

```python
items = [re.sub(r"\\([\\|])", r"\1", item)
         for item in re.findall(r"(?:\\.|[^|])+", cell)]
```

Progress shows attempted files, percentage, files left, unique plants, merged
records, failed files, documents/sec, MB/sec, tokens/sec, elapsed time, and ETA.
Tokens are whitespace-separated words in the extracted fields before merging,
excluding IDs and URLs. MB means 1,000,000 input bytes. Rates include file reading,
extraction, and merging. Writing has a separate progress phase.

Errors and canonical-page warnings go to `data/plants.errors.tsv` (or a sibling
of the chosen output). The final TSV replaces its destination only after the
whole export succeeds. Ctrl+C stops the run; a rerun starts from the beginning.

Run the small correctness checks with:

```powershell
python -B -m unittest -v test_extract_tsv.py
```
