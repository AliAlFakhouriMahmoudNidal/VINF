# VINF

Extract the saved RHS HTML using standard-library Python:

```powershell
python -u extract_tsv.py
```

Settings are defined at the start of `main()` in `extract_tsv.py`:

```python
input_dir = Path("data/html")
output = Path("data/plants.tsv")
readers = 1
limit = None
```

The extractor reads one HTML file at a time and keeps only extracted records in
memory. It uses `re` for field extraction. The standard-library `json` module
only decodes escaped characters in individual strings found by regex.
There are no third-party dependencies. The default uses one thread and process.
Setting `readers = 4` reads and extracts small batches of files concurrently.
Merging stays in the main thread, in the order the folder returns the files.

Set `limit = 10000` to test on the first 10,000 files returned by the folder.
You can also change `output` to `Path("data/plants-test.tsv")` for a sample run.
Run `python extract_tsv.py` after editing the settings; no arguments are needed.

With `limit = None`, all files are processed. The limit counts input files;
canonical merging can produce fewer output rows. Progress counts
the selected files. A limit larger than the available files simply
processes all available files.

The TSV has these columns, in order:

```text
canonical_id, source_url, primary_name, alternative_names, genus, family,
description, plant_range, plant_types, foliage, habit, soil_types, moisture,
exposure, position
```

Each canonical plant gets one row. The canonical page supplies preferred values;
related records contribute alternative names and fill missing values. If its
canonical page is absent, the first related record supplies the name.
Empty descriptions stay empty.

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

Progress prints `Parsed 1,000/10,000 files` every 1,000 files and at the end.
Reading or parsing errors stop the run and show a Python traceback.
The final TSV replaces its destination only after the whole export succeeds.
Ctrl+C stops the run; a rerun starts from the beginning.
