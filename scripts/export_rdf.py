#!/usr/bin/env python3
"""Convert britishempire_kg_export.cypher to CIDOC-CRM RDF/Turtle.

This is the RDF *publication layer* proposed in the paper's "Why not CIDOC-CRM?"
section: the Cypher property graph stays the working model, and this script
emits a LINCS-compatible Turtle serialization for the linked-data ecosystem.

Mapping (see footnotes 99/105/109 of the paper):
  - each HistoricalTerritory  -> crm:E74_Group
  - established/start dates   -> crm:E66_Formation  + crm:E52_Time-Span
  - end/independence dates    -> crm:E68_Dissolution + crm:E52_Time-Span
  - territorial transitions   -> crm:E81_Transformation (n-ary: P124_transformed
                                 inputs, P123_resulted_in outputs), the specific
                                 kind carried by crm:P2_has_type
  - ADMINISTERED_UNDER/PART_OF -> crm:P107i_is_current_or_former_member_of
  - WAS_MEMBER_OF / dated PART_OF -> + crm:E85_Joining / crm:E86_Leaving
  - BORDERS_WITH / NEAR_COAST_OF -> dropped (no geometry in the dataset)

Wikidata grounding: a territory's subject URI *is* its Wikidata entity URI when
the QID is a clean exact match (no qid_scope_note, not a modern-nation stand-in);
otherwise a URI is minted under the base and the QID is attached as
skos:closeMatch / skos:relatedMatch.

Type concepts (colony types, node subtype labels, transition kinds, detail
codes) are minted as local skos:Concept / crm:E55_Type nodes with rdfs:label
only -- grounding them to Wikidata/AAT is a deliberate follow-up pass.

Usage:
  python3 scripts/export_rdf.py
  python3 scripts/export_rdf.py --base-uri http://temp.lincsproject.ca/empire-evolution/
  python3 scripts/export_rdf.py --out data/empire-evolution-crm.ttl
"""
from __future__ import annotations

import argparse
import re
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

REPO_DIR = Path(__file__).resolve().parents[1]
DEFAULT_CYPHER = REPO_DIR / "data" / "britishempire_kg_export.cypher"
DEFAULT_OUT = REPO_DIR / "data" / "empire-evolution-crm.ttl"
DEFAULT_BASE = "https://jimclifford.ca/empire-evolution-wpcs/"

WIKIDATA = "http://www.wikidata.org/entity/"
AAT = "http://vocab.getty.edu/aat/"

# ── relationship classification ──

# transitions modelled as crm:E81_Transformation, with the kind on P2_has_type
TRANSITION_RELS = {
    "EVOLVED_INTO", "SUCCEEDED", "PARTITIONED_INTO", "MERGED_INTO",
    "FEDERATED_INTO", "REUNITED_INTO", "INCORPORATED_INTO", "REORGANIZED_AS",
    "TRANSFERRED_SOVEREIGNTY", "TRANSFERRED_TERRITORY", "BECAME_INDEPENDENT",
    "BECAME_CROWN_COLONY", "BECAME_MANDATE", "BECAME_COLONY",
    "BECAME_PROTECTORATE", "BECAME_SEPARATE_COLONY",
}
# many predecessors -> one successor
MANY_TO_ONE = {"MERGED_INTO", "FEDERATED_INTO", "REUNITED_INTO", "INCORPORATED_INTO"}
# one predecessor -> many successors
ONE_TO_MANY = {"PARTITIONED_INTO"}
# SUCCEEDED is many->one only when its detail marks a confederation
SUCCEEDED_MANY_TO_ONE_DETAILS = {"CONFEDERATED_INTO", "FEDERATION_SUCCESSION"}
# concurrent-governance / spatial-inclusion relations -> P107i (no event node)
MEMBER_RELS = {"ADMINISTERED_UNDER", "PART_OF", "WAS_MEMBER_OF"}
# spatial relations with no clean CRM pattern absent geometry -> dropped
DROP_RELS = {"BORDERS_WITH", "NEAR_COAST_OF"}

# rel type -> (concept slug, human label) for the P2_has_type on the E81 event
TRANSITION_CONCEPT = {
    "EVOLVED_INTO": ("evolution", "Evolution into successor configuration"),
    "SUCCEEDED": ("succession", "Succession"),
    "PARTITIONED_INTO": ("partition", "Partition"),
    "MERGED_INTO": ("merger", "Merger"),
    "FEDERATED_INTO": ("federation", "Federation"),
    "REUNITED_INTO": ("reunification", "Reunification"),
    "INCORPORATED_INTO": ("incorporation", "Incorporation"),
    "REORGANIZED_AS": ("reorganization", "Administrative reorganization"),
    "TRANSFERRED_SOVEREIGNTY": ("sovereignty-transfer", "Transfer of sovereignty"),
    "TRANSFERRED_TERRITORY": ("territory-transfer", "Transfer of territory"),
    "BECAME_INDEPENDENT": ("independence", "Became independent"),
    "BECAME_CROWN_COLONY": ("became-crown-colony", "Became crown colony"),
    "BECAME_MANDATE": ("became-mandate", "Became mandate"),
    "BECAME_COLONY": ("became-colony", "Became colony"),
    "BECAME_PROTECTORATE": ("became-protectorate", "Became protectorate"),
    "BECAME_SEPARATE_COLONY": ("became-separate-colony", "Became separate colony"),
}

# ── Cypher parsing ──

NODE_MERGE_RE = re.compile(
    r"^MERGE \(c:(HistoricalTerritory[A-Za-z:]*) \{colony_id: '([^']+)'\}\)$"
)
NODE_PROP_RE = re.compile(r"^  ([a-z_]+): (.*?),?$")
EDGE_RE = re.compile(
    r"^MATCH \(a:HistoricalTerritory \{colony_id: '([^']+)'\}\), "
    r"\(b:HistoricalTerritory \{colony_id: '([^']+)'\}\) "
    r"MERGE \(a\)-\[:([A-Z_]+)(?: \{(.*)\})?\]->\(b\);$"
)


def _unquote(s: str) -> str:
    s = s.strip()
    if len(s) >= 2 and s[0] == s[-1] and s[0] in "'\"":
        s = s[1:-1]
    return s.replace("\\'", "'").replace('\\"', '"')


def node_value(raw: str):
    """Coerce a node property value from its Cypher literal form."""
    raw = raw.strip()
    if raw.startswith("[") and raw.endswith("]"):
        inner = raw[1:-1].strip()
        return [_unquote(x) for x in inner.split(",")] if inner else []
    if raw.startswith("datetime("):
        m = re.search(r"'([^']*)'", raw)
        return m.group(1) if m else raw
    if raw in ("true", "false"):
        return raw == "true"
    if re.fullmatch(r"-?\d+", raw):
        return int(raw)
    return _unquote(raw)


def parse_prop_map(body: str) -> dict:
    """Parse a Cypher property-map body 'k: v, k: v' (quote-aware)."""
    props: dict = {}
    i, n = 0, len(body)
    while i < n:
        while i < n and body[i] in " ,":
            i += 1
        if i >= n:
            break
        j = i
        while j < n and body[j] != ":":
            j += 1
        key = body[i:j].strip()
        i = j + 1
        while i < n and body[i] == " ":
            i += 1
        if i < n and body[i] == "'":
            i += 1
            chars = []
            while i < n:
                c = body[i]
                if c == "\\" and i + 1 < n:
                    chars.append(body[i + 1])
                    i += 2
                    continue
                if c == "'":
                    i += 1
                    break
                chars.append(c)
                i += 1
            props[key] = "".join(chars)
        else:
            j = i
            while j < n and body[j] != ",":
                j += 1
            raw = body[i:j].strip()
            i = j
            if raw in ("true", "false"):
                props[key] = raw == "true"
            elif re.fullmatch(r"-?\d+", raw):
                props[key] = int(raw)
            else:
                props[key] = raw
    return props


@dataclass
class Node:
    colony_id: str
    labels: list  # subtype labels, excluding HistoricalTerritory
    props: dict


@dataclass
class Edge:
    a: str
    b: str
    rel: str
    props: dict
    lineno: int


def parse_cypher(text: str):
    nodes: dict = {}
    edges: list = []
    cur: Node | None = None
    for lineno, line in enumerate(text.splitlines(), 1):
        if cur is not None:
            if line.startswith("};"):
                nodes[cur.colony_id] = cur
                cur = None
                continue
            pm = NODE_PROP_RE.match(line)
            if pm:
                cur.props[pm.group(1)] = node_value(pm.group(2))
            continue
        nm = NODE_MERGE_RE.match(line)
        if nm:
            labels = [l for l in nm.group(1).split(":") if l != "HistoricalTerritory"]
            cur = Node(colony_id=nm.group(2), labels=labels, props={})
            continue
        em = EDGE_RE.match(line)
        if em:
            props = parse_prop_map(em.group(4)) if em.group(4) else {}
            edges.append(Edge(em.group(1), em.group(2), em.group(3), props, lineno))
    return nodes, edges


# ── dates ──

def normalize_date(raw):
    """Return (precision, label, begin_dt, end_dt) or None.

    precision is 'date' (ISO yyyy-mm-dd) or 'year' (bare year).
    """
    if raw is None or raw == "":
        return None
    s = str(raw).strip()
    m = re.fullmatch(r"(-?\d{1,4})-(\d{2})-(\d{2})", s)
    if m:
        return ("date", s, f"{s}T00:00:00", f"{s}T23:59:59")
    m = re.fullmatch(r"-?\d{1,4}", s)
    if m:
        y = int(s)
        if y < 1:
            # BCE / year-zero: keep the human-readable label only; xsd:dateTime
            # bounds with negative years trip up common parsers.
            return ("year", s, None, None)
        return ("year", s, f"{y:04d}-01-01T00:00:00", f"{y:04d}-12-31T23:59:59")
    return None


def formation_date(node: Node):
    for key in ("start_date", "established_year", "dynasty_founded"):
        nd = normalize_date(node.props.get(key))
        if nd:
            return nd, key
    return None, None


def dissolution_date(node: Node):
    if node.colony_id.endswith("_ongoing"):
        return None, None
    for key in ("end_date", "independence_year"):
        nd = normalize_date(node.props.get(key))
        if nd:
            return nd, key
    return None, None


def year_of(nd):
    """Extract a 4-char-ish year string from a normalize_date() tuple."""
    if not nd:
        return None
    label = nd[1]
    m = re.match(r"(-?\d{1,4})", label)
    return m.group(1) if m else None


# ── QID grounding ──

def classify_qid(node: Node):
    """Return (kind, qid). kind in sameAs|closeMatch|relatedMatch|none."""
    qid = node.props.get("wikidata_id") or ""
    if not qid:
        return ("none", None)
    scope = (node.props.get("qid_scope_note") or "").strip()
    if scope:
        if scope.startswith("[DATE_RANGE_MISMATCH]"):
            return ("closeMatch", qid)
        # [QID_REUSED], [QID_REUSED_TOO_BROAD], or a bare descriptive note: the
        # QID stands in for a different / broader entity, not an identity match.
        return ("relatedMatch", qid)
    if node.props.get("qid_type") == "modern_nation":
        return ("closeMatch", qid)
    return ("sameAs", qid)


# ── Turtle helpers ──

def ttl_str(s: str) -> str:
    s = (s.replace("\\", "\\\\").replace('"', '\\"')
          .replace("\n", "\\n").replace("\r", "\\r").replace("\t", "\\t"))
    return f'"{s}"'


def lit(s: str) -> str:
    return ttl_str(s) + "@en"


def typed(value: str, dtype: str) -> str:
    return f"{ttl_str(value)}^^{dtype}"


class Emitter:
    """Accumulates entity blocks into named sections, emits sorted Turtle."""

    def __init__(self, base: str):
        self.base = base
        self.sections: dict = defaultdict(dict)  # section -> {uri: [lines]}

    def mint(self, path: str) -> str:
        return f"<{self.base}{path}>"

    def add(self, section: str, uri: str, lines: list):
        # later writes win for the same uri (idempotent); extra triples should
        # be merged explicitly by the caller instead of relying on this.
        self.sections[section][uri] = lines

    def has(self, section: str, uri: str) -> bool:
        return uri in self.sections[section]

    def render(self) -> str:
        order = [
            ("prefixes", "Namespace prefixes"),
            ("group", "Territories (E74 Group)"),
            ("appellation", "Names (E33_E41 Linguistic Appellation)"),
            ("formation", "Formation events (E66 Formation)"),
            ("dissolution", "Dissolution events (E68 Dissolution)"),
            ("transformation", "Territorial transitions (E81 Transformation)"),
            ("joining", "Joining events (E85 Joining)"),
            ("leaving", "Leaving events (E86 Leaving)"),
            ("timespan", "Time-spans (E52 Time-Span)"),
            ("vocab", "Type vocabulary (E55 Type / skos:Concept) -- ground to "
                      "Wikidata/AAT in a follow-up pass"),
        ]
        out = []
        for section, title in order:
            entries = self.sections.get(section)
            if not entries:
                continue
            out.append(f"# {'=' * 70}")
            out.append(f"# {title}")
            out.append(f"# {'=' * 70}\n")
            for uri in sorted(entries):
                out.extend(entries[uri])
                out.append("")
        return "\n".join(out) + "\n"


PREFIXES = [
    ("crm", "http://www.cidoc-crm.org/cidoc-crm/"),
    ("rdf", "http://www.w3.org/1999/02/22-rdf-syntax-ns#"),
    ("rdfs", "http://www.w3.org/2000/01/rdf-schema#"),
    ("xsd", "http://www.w3.org/2001/XMLSchema#"),
    ("owl", "http://www.w3.org/2002/07/owl#"),
    ("skos", "http://www.w3.org/2004/02/skos/core#"),
    ("wikidata", WIKIDATA),
    ("aat", AAT),
]


def slug(s: str) -> str:
    s = s.strip().lower()
    s = re.sub(r"[^a-z0-9]+", "-", s)
    return s.strip("-")


# ── conversion ──

class Converter:
    def __init__(self, base: str):
        self.base = base
        self.em = Emitter(base)
        self.uri: dict = {}        # colony_id -> turtle term for the E74 group
        self.member_triples: dict = defaultdict(list)  # colony_id -> [lines]
        self.stats: dict = defaultdict(int)
        self.warnings: list = []

    # -- vocabulary --

    def vocab(self, kind: str, slug_val: str, label: str) -> str:
        """Mint (once) a local E55_Type / skos:Concept and return its URI."""
        uri = self.em.mint(f"vocab/{kind}/{slug_val}")
        if not self.em.has("vocab", uri):
            self.em.add("vocab", uri, [
                f"{uri} a crm:E55_Type, skos:Concept ;",
                f"    rdfs:label {lit(label)} ;",
                f"    skos:prefLabel {lit(label)} .",
                "    # FILL ME: add skos:exactMatch / skos:closeMatch to "
                "Wikidata or Getty AAT",
            ])
        return uri

    # -- time-spans --

    def timespan(self, uri: str, nd) -> str:
        """Emit an E52_Time-Span from a normalize_date() tuple; return its URI."""
        _precision, label, begin, end = nd
        lines = [
            f"{uri} a crm:E52_Time-Span ;",
            f"    rdfs:label {lit(label)} ;",
            f"    crm:P82_at_some_time_within {typed(label, 'xsd:string')} ;",
        ]
        if begin is not None and end is not None:
            lines.append(f"    crm:P82a_begin_of_the_begin {typed(begin, 'xsd:dateTime')} ;")
            lines.append(f"    crm:P82b_end_of_the_end {typed(end, 'xsd:dateTime')} ;")
        lines[-1] = lines[-1].rstrip(" ;") + " ."
        self.em.add("timespan", uri, lines)
        return uri

    # -- territory nodes --

    def resolve_uris(self, nodes: dict):
        """Decide each territory's subject URI; guard against QID collisions."""
        sameas_owner: dict = {}
        for cid, node in nodes.items():
            kind, qid = classify_qid(node)
            if kind == "sameAs":
                sameas_owner.setdefault(qid, []).append(cid)
        collided = {q for q, cids in sameas_owner.items() if len(cids) > 1}
        for q in sorted(collided):
            self.warnings.append(
                f"QID collision: {q} is a clean match for "
                f"{', '.join(sorted(sameas_owner[q]))} -- minting URIs for all"
            )
        for cid, node in nodes.items():
            kind, qid = classify_qid(node)
            if kind == "sameAs" and qid not in collided:
                self.uri[cid] = f"wikidata:{qid}"
            else:
                self.uri[cid] = self.em.mint(cid)

    def convert_node(self, node: Node):
        cid = node.colony_id
        uri = self.uri[cid]
        name = node.props.get("name") or node.props.get("canonical_name") or cid
        lines = [
            f"{uri} a crm:E74_Group ;",
            f"    rdfs:label {lit(name)} ;",
        ]
        # name appellation(s)
        appel = self.em.mint(f"{cid}/name")
        lines.append(f"    crm:P1_is_identified_by {appel} ;")
        self.em.add("appellation", appel, [
            f"{appel} a crm:E33_E41_Linguistic_Appellation ;",
            f"    rdfs:label {lit('Name of ' + name)} ;",
            f"    crm:P2_has_type {self.vocab('name-type', 'territory-name', 'Territory name')} ;",
            f"    crm:P190_has_symbolic_content {typed(name, 'xsd:string')} .",
        ])
        canon = node.props.get("canonical_name")
        if canon and canon != name:
            curi = self.em.mint(f"{cid}/canonical-name")
            lines.append(f"    crm:P1_is_identified_by {curi} ;")
            self.em.add("appellation", curi, [
                f"{curi} a crm:E33_E41_Linguistic_Appellation ;",
                f"    rdfs:label {lit('Canonical name of ' + name)} ;",
                f"    crm:P2_has_type {self.vocab('name-type', 'canonical-name', 'Canonical name')} ;",
                f"    crm:P190_has_symbolic_content {typed(canon, 'xsd:string')} .",
            ])
        # types: colony_type, subtype labels, whg_aat_types
        ctype = node.props.get("colony_type")
        if ctype:
            lines.append(f"    crm:P2_has_type {self.vocab('type', slug(ctype), ctype)} ;")
        for label in node.labels:
            lines.append(f"    crm:P2_has_type {self.vocab('label', label, label)} ;")
        for aat_id in node.props.get("whg_aat_types") or []:
            if re.fullmatch(r"\d+", str(aat_id)):
                lines.append(f"    crm:P2_has_type aat:{aat_id} ;")
        # formation / dissolution back-links
        f_nd, f_key = formation_date(node)
        if f_nd:
            lines.append(f"    crm:P95i_was_formed_by {self.em.mint(cid + '/formation')} ;")
        d_nd, d_key = dissolution_date(node)
        if d_nd:
            lines.append(f"    crm:P99i_was_dissolved_by {self.em.mint(cid + '/dissolution')} ;")
        # QID grounding link
        kind, qid = classify_qid(node)
        if kind == "closeMatch":
            lines.append(f"    skos:closeMatch wikidata:{qid} ;")
        elif kind == "relatedMatch":
            lines.append(f"    skos:relatedMatch wikidata:{qid} ;")
        for mq in node.props.get("modern_nation_qids") or []:
            if re.fullmatch(r"Q\d+", str(mq)) and mq != qid:
                lines.append(f"    skos:relatedMatch wikidata:{mq} ;")
        # scope note travels with the data
        scope = (node.props.get("qid_scope_note") or "").strip()
        if scope:
            lines.append(f"    rdfs:comment {lit('Wikidata QID scope note: ' + scope)} ;")
        comments = (node.props.get("comments") or "").strip()
        if comments:
            lines.append(f"    rdfs:comment {lit(comments)} ;")
        # member relations collected from edges (P107i)
        lines.extend(self.member_triples.get(cid, []))
        lines[-1] = lines[-1].rstrip(" ;") + " ."
        self.em.add("group", uri, lines)
        # formation / dissolution events
        if f_nd:
            self._formation_event(node, f_nd, f_key)
        if d_nd:
            self._dissolution_event(node, d_nd)

    def _formation_event(self, node: Node, nd, key: str):
        cid = node.colony_id
        name = node.props.get("name") or cid
        ev = self.em.mint(f"{cid}/formation")
        ts = self.timespan(self.em.mint(f"{cid}/formation/timespan"), nd)
        lines = [
            f"{ev} a crm:E66_Formation ;",
            f"    rdfs:label {lit('Formation of ' + name)} ;",
            f"    crm:P95_has_formed {self.uri[cid]} ;",
            f"    crm:P4_has_time-span {ts} ;",
        ]
        if key == "dynasty_founded":
            # dynasty founding != the polity's entry into the imperial system;
            # type the event so the weaker semantics are explicit.
            v = self.vocab("transition", "dynasty-founding", "Dynasty founding")
            lines.append(f"    crm:P2_has_type {v} ;")
        lines[-1] = lines[-1].rstrip(" ;") + " ."
        self.em.add("formation", ev, lines)

    def _dissolution_event(self, node: Node, nd):
        cid = node.colony_id
        name = node.props.get("name") or cid
        ev = self.em.mint(f"{cid}/dissolution")
        ts = self.timespan(self.em.mint(f"{cid}/dissolution/timespan"), nd)
        self.em.add("dissolution", ev, [
            f"{ev} a crm:E68_Dissolution ;",
            f"    rdfs:label {lit('Dissolution of ' + name)} ;",
            f"    crm:P99_was_dissolved {self.uri[cid]} ;",
            f"    crm:P4_has_time-span {ts} .",
        ])

    # -- edges --

    def partition_edges(self, edges: list, nodes: dict):
        transitions, members, dropped = [], [], 0
        for e in edges:
            if e.a not in nodes or e.b not in nodes:
                self.warnings.append(
                    f"line {e.lineno}: edge endpoint missing from node set "
                    f"({e.a} -[{e.rel}]-> {e.b}) -- skipped")
                continue
            if e.a == e.b:
                self.warnings.append(f"line {e.lineno}: self-loop {e.a} -[{e.rel}]- -- skipped")
                continue
            if e.rel in DROP_RELS:
                dropped += 1
            elif e.rel in MEMBER_RELS:
                members.append(e)
            elif e.rel in TRANSITION_RELS:
                transitions.append(e)
            else:
                self.warnings.append(f"line {e.lineno}: unmapped rel type {e.rel} -- skipped")
        return transitions, members, dropped

    def _direction(self, e: Edge):
        """Normalize to (input, output). SUCCEEDED is stored reversed."""
        if e.rel == "SUCCEEDED":
            return e.b, e.a
        return e.a, e.b

    def _cardinality(self, e: Edge):
        if e.rel in ONE_TO_MANY:
            return "1_many"
        if e.rel in MANY_TO_ONE:
            return "many_1"
        if e.rel == "SUCCEEDED" and e.props.get("detail") in SUCCEEDED_MANY_TO_ONE_DETAILS:
            return "many_1"
        return "1_1"

    def convert_transitions(self, edges: list, nodes: dict):
        groups: dict = defaultdict(list)
        for e in edges:
            inp, out = self._direction(e)
            card = self._cardinality(e)
            year = e.props.get("year")
            if year is not None:
                ykey = str(year)
            elif card == "1_many":
                ykey = year_of(dissolution_date(nodes[inp])[0]) or f"_line{e.lineno}"
            elif card == "many_1":
                ykey = year_of(formation_date(nodes[out])[0]) or f"_line{e.lineno}"
            else:
                ykey = f"_line{e.lineno}"
            if card == "1_many":
                key = (e.rel, ykey, "in", inp)
            elif card == "many_1":
                key = (e.rel, ykey, "out", out)
            else:
                key = (e.rel, ykey, "11", inp, out)
            groups[key].append((inp, out, e))
        for key in sorted(groups, key=lambda k: tuple(map(str, k))):
            self._emit_transformation(key, groups[key], nodes)
        self.stats["transformation_events"] = len(groups)
        self.stats["transformation_edges"] = sum(len(v) for v in groups.values())
        self.stats["transformation_nary"] = sum(
            1 for v in groups.values()
            if len({i for i, _o, _e in v}) > 1 or len({o for _i, o, _e in v}) > 1
        )

    def _emit_transformation(self, key, members, nodes):
        rel = key[0]
        ykey = key[1]
        inputs = sorted({i for i, _o, _e in members})
        outputs = sorted({o for _i, o, _e in members})
        # real year for the time-span: prefer an explicit edge year
        year = None
        for _i, _o, e in members:
            if e.props.get("year") is not None:
                year = str(e.props["year"])
                break
        if year is None and not ykey.startswith("_line"):
            year = ykey
        concept_slug, concept_label = TRANSITION_CONCEPT[rel]
        anchor = key[3] if key[2] in ("in", "out") else f"{key[3]}--{key[4]}"
        ev = self.em.mint(f"transition/{concept_slug}/{ykey.lstrip('_')}/{anchor}")
        in_names = ", ".join(nodes[i].props.get("name", i) for i in inputs)
        out_names = ", ".join(nodes[o].props.get("name", o) for o in outputs)
        label = f"{concept_label}: {in_names} → {out_names}"
        if len(label) > 200:
            label = label[:197] + "..."
        lines = [
            f"{ev} a crm:E81_Transformation ;",
            f"    rdfs:label {lit(label)} ;",
        ]
        for i in inputs:
            lines.append(f"    crm:P124_transformed {self.uri[i]} ;")
        for o in outputs:
            lines.append(f"    crm:P123_resulted_in {self.uri[o]} ;")
        lines.append(f"    crm:P2_has_type {self.vocab('transition', concept_slug, concept_label)} ;")
        # finer-grained detail / succession-type codes
        details = sorted({e.props["detail"] for _i, _o, e in members if e.props.get("detail")})
        for d in details:
            lines.append(f"    crm:P2_has_type {self.vocab('detail', slug(d), d.replace('_', ' ').title())} ;")
        stypes = sorted({e.props["succession_type"] for _i, _o, e in members
                         if e.props.get("succession_type")})
        for st in stypes:
            lines.append(f"    crm:P2_has_type {self.vocab('succession-type', slug(st), st.replace('_', ' ').title())} ;")
        if year is not None:
            nd = normalize_date(year)
            if nd:
                ts = self.timespan(self.em.mint(
                    f"transition/{concept_slug}/{ykey.lstrip('_')}/{anchor}/timespan"), nd)
                lines.append(f"    crm:P4_has_time-span {ts} ;")
        descs = sorted({e.props["description"] for _i, _o, e in members if e.props.get("description")})
        for d in descs:
            lines.append(f"    rdfs:comment {lit(d)} ;")
        srcs = sorted({e.props["source"] for _i, _o, e in members if e.props.get("source")})
        for s in srcs:
            lines.append(f"    rdfs:comment {lit('source: ' + str(s))} ;")
        lines[-1] = lines[-1].rstrip(" ;") + " ."
        self.em.add("transformation", ev, lines)

    def convert_members(self, edges: list, nodes: dict):
        for e in edges:
            # LINCS convention: P107 on the group (b), pointing at the member (a)
            self.member_triples[e.b].append(
                f"    crm:P107_has_current_or_former_member {self.uri[e.a]} ;")
            start = e.props.get("start_year")
            end = e.props.get("end_year")
            a_name = nodes[e.a].props.get("name", e.a)
            b_name = nodes[e.b].props.get("name", e.b)
            if start is not None:
                nd = normalize_date(start)
                if nd:
                    ev = self.em.mint(f"{e.a}/joined/{e.b}")
                    ts = self.timespan(self.em.mint(f"{e.a}/joined/{e.b}/timespan"), nd)
                    self.em.add("joining", ev, [
                        f"{ev} a crm:E85_Joining ;",
                        f"    rdfs:label {lit(a_name + ' joined ' + b_name)} ;",
                        f"    crm:P143_joined {self.uri[e.a]} ;",
                        f"    crm:P144_joined_with {self.uri[e.b]} ;",
                        f"    crm:P4_has_time-span {ts} .",
                    ])
            if end is not None:
                nd = normalize_date(end)
                if nd:
                    ev = self.em.mint(f"{e.a}/left/{e.b}")
                    ts = self.timespan(self.em.mint(f"{e.a}/left/{e.b}/timespan"), nd)
                    self.em.add("leaving", ev, [
                        f"{ev} a crm:E86_Leaving ;",
                        f"    rdfs:label {lit(a_name + ' left ' + b_name)} ;",
                        f"    crm:P145_separated {self.uri[e.a]} ;",
                        f"    crm:P146_separated_from {self.uri[e.b]} ;",
                        f"    crm:P4_has_time-span {ts} .",
                    ])

    # -- orchestration --

    def run(self, nodes: dict, edges: list):
        self.resolve_uris(nodes)
        transitions, members, dropped = self.partition_edges(edges, nodes)
        # members first: they contribute P107i triples onto the group blocks
        self.convert_members(members, nodes)
        for cid in sorted(nodes):
            self.convert_node(nodes[cid])
        self.convert_transitions(transitions, nodes)
        self.stats["nodes"] = len(nodes)
        self.stats["edges_total"] = len(edges)
        self.stats["edges_member"] = len(members)
        self.stats["edges_dropped"] = dropped

    def render(self, base: str) -> str:
        header = [
            "# British Empire Knowledge Graph -- CIDOC-CRM RDF/Turtle",
            "# Generated by scripts/export_rdf.py from "
            "data/britishempire_kg_export.cypher",
            "# Publication layer; the Cypher property graph remains the working model.",
            f"# Base URI for minted entities: {base}",
            "",
        ]
        prefix_lines = [f"@prefix {p}: <{u}> ." for p, u in PREFIXES]
        self.em.sections["prefixes"]["_"] = prefix_lines
        # the prefixes section renderer would add a redundant title; emit manually
        body = self.em.render()
        return "\n".join(header) + "\n" + body


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--cypher", default=str(DEFAULT_CYPHER))
    ap.add_argument("--out", default=str(DEFAULT_OUT))
    ap.add_argument("--base-uri", default=DEFAULT_BASE,
                    help=f"base URI for minted entities (default: {DEFAULT_BASE})")
    args = ap.parse_args()

    base = args.base_uri if args.base_uri.endswith("/") else args.base_uri + "/"
    text = Path(args.cypher).read_text()
    nodes, edges = parse_cypher(text)

    conv = Converter(base)
    conv.run(nodes, edges)
    ttl = conv.render(base)
    Path(args.out).write_text(ttl)

    s = conv.stats
    print(f"parsed {s['nodes']} territories, {s['edges_total']} relationships", file=sys.stderr)
    print(f"  transition edges:  {s['transformation_edges']:>5} "
          f"-> {s['transformation_events']} E81_Transformation events "
          f"({s['transformation_nary']} n-ary)", file=sys.stderr)
    print(f"  member edges:      {s['edges_member']:>5} -> P107i / E85 / E86", file=sys.stderr)
    print(f"  dropped (spatial): {s['edges_dropped']:>5}", file=sys.stderr)
    accounted = s["transformation_edges"] + s["edges_member"] + s["edges_dropped"]
    print(f"  accounted for:     {accounted} / {s['edges_total']}", file=sys.stderr)
    if conv.warnings:
        print(f"\n{len(conv.warnings)} warning(s):", file=sys.stderr)
        for w in conv.warnings:
            print(f"  - {w}", file=sys.stderr)
    print(f"\nwrote {args.out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
