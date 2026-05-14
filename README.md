# empire-evolution-wpcs

A Cypher knowledge graph and interactive visualization of British imperial
territorial evolution, published in *Working Papers in Critical Search* (WPCS)
as Paper № 002.

## Contents

- `index.qmd` — Quarto source for the paper
- `references.bib` — bibliography
- `data/britishempire_kg_export.cypher` — full graph: 747 historical
  territories (314 colonial polities + 433 princely states) and 1,203 typed
  relationships
- `viz/empire_evolution.html` — self-contained D3.js Sankey visualization
- `scripts/load_falkordb.py` — load the graph into an embedded FalkorDB instance
  (no Docker, no Neo4j server)
- `notebooks/quick_tour.ipynb` — guided tour with three pyvis visualizations
  (label/edge stats, Canada lineage tree, Southeast Asia subgraph)

## Quick start: spin up the graph in-process

```bash
pip install -r requirements.txt
python3 scripts/load_falkordb.py --interactive
```

This loads the 747 nodes / 1,203 edges into an embedded FalkorDB
("FalkorDBLite") instance saved to `./empire.db` and drops you into a
Cypher REPL. No Neo4j installation, no Docker, no Java.

Sample query at the prompt:

```cypher
MATCH (a:HistoricalTerritory)-[:PARTITIONED_INTO]->(b:HistoricalTerritory)
RETURN a.canonical_name, b.canonical_name, b.established_year
ORDER BY b.established_year LIMIT 20
```

## Quick start: notebook tour

```bash
pip install -r requirements.txt
python3 scripts/load_falkordb.py --reset    # creates ./empire.db
jupyter notebook notebooks/quick_tour.ipynb
```

The notebook builds three visualizations directly from FalkorDB Cypher
queries:

1. Schema-level stats (label and relationship-type breakdowns)
2. Lineage of modern Canada (predecessor tree via `EVOLVED_INTO` /
   `PARTITIONED_INTO`)
3. Southeast Asia regional subgraph (Malaysia formation, Singapore
   separation), colored by colonial type

Both rendered HTML graphs (`notebooks/canada_lineage.html`,
`notebooks/se_asia.html`) are checked in so you can preview without
running.

> **Note**: the canonical export uses Neo4j-flavored Cypher
> (`CREATE CONSTRAINT`, `datetime(...)`, `point({srid: 4326, ...})`). The
> FalkorDB loader silently rewrites these on the fly to match FalkorDB's
> parser; temporal types degrade to ISO strings and points retain
> latitude/longitude only. See `scripts/load_falkordb.py` for the rewrite
> rules.

## Alternative: load into Neo4j

```bash
cypher-shell -u neo4j -p <password> -f data/britishempire_kg_export.cypher
```

Neo4j 5.x is recommended (the export uses `CREATE CONSTRAINT IF NOT EXISTS
FOR ... REQUIRE` syntax). Re-running is idempotent.

## Generate the RDF publication layer

```bash
python3 scripts/export_rdf.py
```

Converts the Cypher graph to CIDOC-CRM RDF/Turtle at
`data/empire-evolution-crm.ttl` — the EHRI-style publication layer described in
the paper's "Why not CIDOC-CRM?" section. Territories become `crm:E74_Group`,
formation/dissolution become `crm:E66_Formation`/`crm:E68_Dissolution` events,
and territorial transitions become n-ary `crm:E81_Transformation` events. A
territory's subject URI *is* its Wikidata entity URI when the QID is a clean
exact match; otherwise a URI is minted and the QID attached as a `skos`
match. The script has no dependencies; `pip install rdflib` only to
parse-check the output:

```bash
python3 -c "import rdflib; print(len(rdflib.Graph().parse('data/empire-evolution-crm.ttl')))"
```

Use `--base-uri` to override the namespace for minted entities.

## Open the visualization

```bash
xdg-open viz/empire_evolution.html      # Linux
open viz/empire_evolution.html          # macOS
```

The data is embedded in the HTML; no server needed.

## Render the paper

```bash
quarto render
```

Renders the Quarto website (HTML) into `_site/`. The journal chrome is
injected via `_includes/` and the theme stack in `_quarto.yml`.

## Citation

```
Clifford, Jim. (2026). A Cypher Knowledge Graph of British Imperial
Territorial Evolution. Working Papers in Critical Search.
```

(Update with version tag and DOI on publication.)

## License

This repository is licensed in three parts, each under the license best
suited to it:

- **Paper text** (`index.qmd`, `references.bib`, `images/`): CC-BY 4.0
- **Dataset** (`data/`, `viz/`): CC0 1.0 (public domain dedication).
  Attribution is appreciated but not required.
- **Code** (`scripts/`, `notebooks/`): MIT

See `LICENSE` for full terms.
