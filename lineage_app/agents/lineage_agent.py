"""LineageAgent for building and querying a NetworkX graph of data lineage."""

import logging
from typing import Dict, List, Any, Optional

import networkx as nx
from . import normalise_table

logger = logging.getLogger(__name__)


class LineageAgent:
    """
    Consumes the list of SP-analysis dicts from SPAgent.normalised_results() and
    builds a NetworkX directed graph (nx.DiGraph). Provides all query methods
    needed by the Flask API.
    """

    def __init__(self, normalised_sp_results: List[Dict[str, Any]]):
        """
        Initialize the LineageAgent with normalised SP results.

        Args:
            normalised_sp_results: List of SPAgent results with normalised table names.
        """
        self.graph = nx.DiGraph()
        self._build_graph(normalised_sp_results)

    def _build_graph(self, results: List[Dict[str, Any]]) -> None:
        """
        Build the directed graph from the normalised SP results.

        All node names are normalised so that lookups via normalise_table()
        always match. The original (pre-normalisation) name is preserved as
        the node's ``display_name`` attribute for use in labels / UI.
        """
        logger.info("Building lineage graph from %d SP results", len(results))

        for result in results:
            raw_target = result.get("target_table")
            if not raw_target:
                continue
            target = normalise_table(raw_target)

            # Ensure target node exists
            self._ensure_node(target, raw_target, result)

            # Process source tables and create edges
            raw_source_tables = result.get("source_tables", [])
            for raw_source in raw_source_tables:
                if not raw_source:
                    continue
                source = normalise_table(raw_source)

                # Ensure source node exists
                self._ensure_node(source, raw_source, result)

                # Add or update edge from source to target
                self._add_or_update_edge(source, target, result)

    def _ensure_node(self, table_name: str, raw_name: str, result: Dict[str, Any]) -> None:
        """
        Ensure a node exists in the graph, creating it if necessary.

        Args:
            table_name: Normalised table name (used as the graph node ID).
            raw_name:   Original name before normalisation (stored for display).
            result:     The SP result dict (unused for now, kept for API compat).
        """
        if self.graph.has_node(table_name):
            # Node already exists — keep the existing node but update display_name
            # if the new raw_name is more informative (longer / has qualifiers).
            existing_display = self.graph.nodes[table_name].get("display_name", "")
            if len(raw_name) > len(existing_display):
                self.graph.nodes[table_name]["display_name"] = raw_name
            return

        # Create new node with default attributes
        self.graph.add_node(
            table_name,
            display_name=raw_name,  # original name for UI labels
            schema="dbo",  # default
            database="",   # default
            qualified_name="",
            columns=[],
            column_details=[],
            in_catalogue=False,
        )

    def _add_or_update_edge(self, source: str, target: str, result: Dict[str, Any]) -> None:
        """
        Add a new edge or update an existing one with data from the SP result.
        """
        if self.graph.has_edge(source, target):
            # Edge exists, merge the data
            edge_data = self.graph[source][target]
            self._merge_edge_data(edge_data, result)
        else:
            # Create new edge
            edge_data = self._create_edge_data(result)
            self.graph.add_edge(source, target, **edge_data)

    def _merge_edge_data(self, edge_data: Dict[str, Any], result: Dict[str, Any]) -> None:
        """
        Merge new data into an existing edge.
        """
        # Merge column_mappings
        existing_mappings = {self._mapping_key(m): m for m in edge_data.get("column_mappings", [])}
        for mapping in result.get("column_mappings", []):
            key = self._mapping_key(mapping)
            if key not in existing_mappings:
                existing_mappings[key] = mapping
        edge_data["column_mappings"] = list(existing_mappings.values())

        # Merge joins
        existing_joins = {self._join_key(j): j for j in edge_data.get("joins", [])}
        for join in result.get("joins", []):
            key = self._join_key(join)
            if key not in existing_joins:
                existing_joins[key] = join
        edge_data["joins"] = list(existing_joins.values())

        # Merge filters (just extend, but avoid duplicates)
        existing_filters = set(edge_data.get("filters", []))
        for filter_expr in result.get("filters", []):
            existing_filters.add(filter_expr)
        edge_data["filters"] = list(existing_filters)

        # Merge grouping (just extend, but avoid duplicates)
        existing_grouping = set(edge_data.get("grouping", []))
        for group_expr in result.get("grouping", []):
            existing_grouping.add(group_expr)
        edge_data["grouping"] = list(existing_grouping)

        # Merge procedures
        existing_procedures = set(edge_data.get("procedures", []))
        procedure_name = result.get("procedure_name", "")
        if procedure_name:
            existing_procedures.add(procedure_name)
        edge_data["procedures"] = list(existing_procedures)

        # Merge extraction_methods
        existing_methods = set(edge_data.get("extraction_methods", []))
        method = result.get("extraction_method", "")
        if method:
            existing_methods.add(method)
        edge_data["extraction_methods"] = list(existing_methods)

    def _create_edge_data(self, result: Dict[str, Any]) -> Dict[str, Any]:
        """
        Create edge data dictionary from an SP result.
        """
        return {
            "column_mappings": result.get("column_mappings", []),
            "joins": result.get("joins", []),
            "filters": result.get("filters", []),
            "grouping": result.get("grouping", []),
            "procedures": [result.get("procedure_name", "")] if result.get("procedure_name") else [],
            "extraction_methods": [result.get("extraction_method", "")] if result.get("extraction_method") else [],
        }

    def _mapping_key(self, mapping: Dict[str, Any]) -> str:
        """
        Create a unique key for a column mapping to detect duplicates.
        """
        return f"{mapping.get('target_column', '')}:{mapping.get('source_table', '')}:{mapping.get('source_column', '')}"

    def _join_key(self, join: Dict[str, Any]) -> str:
        """
        Create a unique key for a join to detect duplicates.
        """
        return f"{join.get('left_table', '')}:{join.get('right_table', '')}:{join.get('join_type', '')}:{join.get('condition', '')}"

    # Node enrichment methods would be called from Flask app after loading catalogue
    def enrich_nodes_from_catalogue(self, catalogue: Dict[str, Dict[str, Any]]) -> None:
        """
        Enrich graph nodes with information from the table catalogue.
        """
        logger.info("Enriching %d nodes with catalogue data", len(self.graph.nodes))
        for table_name in self.graph.nodes:
            if table_name in catalogue:
                info = catalogue[table_name]
                # Update node attributes
                nx.set_node_attributes(self.graph, {
                    table_name: {
                        "schema": info.get("schema", "dbo"),
                        "database": info.get("database", ""),
                        "qualified_name": info.get("qualified_name", ""),
                        "columns": [col["name"] for col in info.get("columns", [])],
                        "column_details": info.get("columns", []),
                        "in_catalogue": True,
                    }
                })
            else:
                # Node not in catalogue, mark as such
                nx.set_node_attributes(self.graph, {
                    table_name: {
                        "in_catalogue": False,
                    }
                })

    # Query methods
    def all_tables(self) -> List[str]:
        """
        Return sorted list of all node names.
        """
        return sorted(self.graph.nodes)

    def get_upstream(self, table: str, depth: int = 5) -> nx.DiGraph:
        """
        BFS following predecessors up to `depth` hops; returns subgraph copy.
        """
        table = normalise_table(table)
        if not self.graph.has_node(table):
            return nx.DiGraph()

        # BFS to get all predecessors up to depth
        visited = {table}
        current_level = {table}
        edges_to_add = []

        for _ in range(depth):
            next_level = set()
            for node in current_level:
                for predecessor in self.graph.predecessors(node):
                    if predecessor not in visited:
                        visited.add(predecessor)
                        next_level.add(predecessor)
                        edges_to_add.append((predecessor, node))
            current_level = next_level
            if not current_level:
                break

        # Create subgraph
        subgraph = nx.DiGraph()
        subgraph.add_nodes_from(visited)
        subgraph.add_edges_from(edges_to_add)

        # Copy node and edge attributes
        for node in visited:
            if self.graph.has_node(node):
                subgraph.nodes[node].update(self.graph.nodes[node])

        for u, v in edges_to_add:
            if self.graph.has_edge(u, v):
                subgraph.edges[u, v].update(self.graph.edges[u, v])

        return subgraph

    def get_downstream(self, table: str, depth: int = 5) -> nx.DiGraph:
        """
        BFS following successors up to `depth` hops; returns subgraph copy.
        """
        table = normalise_table(table)
        if not self.graph.has_node(table):
            return nx.DiGraph()

        # BFS to get all successors up to depth
        visited = {table}
        current_level = {table}
        edges_to_add = []

        for _ in range(depth):
            next_level = set()
            for node in current_level:
                for successor in self.graph.successors(node):
                    if successor not in visited:
                        visited.add(successor)
                        next_level.add(successor)
                        edges_to_add.append((node, successor))
            current_level = next_level
            if not current_level:
                break

        # Create subgraph
        subgraph = nx.DiGraph()
        subgraph.add_nodes_from(visited)
        subgraph.add_edges_from(edges_to_add)

        # Copy node and edge attributes
        for node in visited:
            if self.graph.has_node(node):
                subgraph.nodes[node].update(self.graph.nodes[node])

        for u, v in edges_to_add:
            if self.graph.has_edge(u, v):
                subgraph.edges[u, v].update(self.graph.edges[u, v])

        return subgraph

    def get_lineage(self, table: str, depth: int = 5) -> nx.DiGraph:
        """
        Union of upstream and downstream subgraphs.
        """
        upstream = self.get_upstream(table, depth)
        downstream = self.get_downstream(table, depth)

        # Combine the graphs
        lineage = nx.DiGraph()
        lineage.add_nodes_from(set(upstream.nodes) | set(downstream.nodes))
        lineage.add_edges_from(set(upstream.edges) | set(downstream.edges))

        # Copy attributes
        for node in lineage.nodes:
            if upstream.has_node(node):
                lineage.nodes[node].update(upstream.nodes[node])
            elif downstream.has_node(node):
                lineage.nodes[node].update(downstream.nodes[node])

        for u, v in lineage.edges:
            if upstream.has_edge(u, v):
                lineage.edges[u, v].update(upstream.edges[u, v])
            elif downstream.has_edge(u, v):
                lineage.edges[u, v].update(downstream.edges[u, v])

        return lineage

    def column_lineage(self, table: str) -> Dict[str, List[Dict[str, Any]]]:
        """
        Return column-level lineage for the given table.

        Each incoming entry is a 1:1 copy of one column_mappings record:
        source_table, source_column, target_column, transformation_type, and
        transformation_logic all come from the same mapping object — never mixed
        across mappings or replaced by a loop variable.

        Because the same mapping can appear on multiple predecessor or successor
        edges (the graph-builder copies an SP's full mapping list to every edge
        it creates), we deduplicate by (source_table, source_column, target_column).
        """
        table = normalise_table(table)
        if not self.graph.has_node(table):
            return {"incoming": [], "outgoing": []}

        incoming = []
        outgoing = []
        seen_incoming: set = set()
        seen_outgoing: set = set()

        # Get incoming edges (predecessors)
        for predecessor in self.graph.predecessors(table):
            edge_data = self.graph[predecessor][table]
            for mapping in edge_data.get("column_mappings", []):
                # Use the mapping's own source_table, not the graph predecessor
                source_table = mapping.get("source_table", "") or predecessor
                source_table_qualified = mapping.get("source_table_qualified", "")
                source_column = mapping.get("source_column", "")
                target_column = mapping.get("target_column", "")
                transformation_type = mapping.get("transformation_type", "")
                transformation_logic = mapping.get("transformation_logic", "")

                # Dedup key: same mapping may appear on multiple edges
                key = f"{source_table}|{source_column}|{target_column}"
                if key in seen_incoming:
                    continue
                seen_incoming.add(key)

                incoming.append({
                    "source_table": source_table,
                    "source_table_qualified": source_table_qualified,
                    "source_column": source_column,
                    "target_column": target_column,
                    "transformation_type": transformation_type,
                    "transformation_logic": transformation_logic,
                })

        # Get outgoing edges (successors)
        for successor in self.graph.successors(table):
            edge_data = self.graph[table][successor]
            for mapping in edge_data.get("column_mappings", []):
                target_table = mapping.get("target_table", "") or successor
                target_table_qualified = mapping.get("target_table_qualified", "")
                source_table = mapping.get("source_table", "")
                source_table_qualified = mapping.get("source_table_qualified", "")
                source_column = mapping.get("source_column", "")
                target_column = mapping.get("target_column", "")
                transformation_type = mapping.get("transformation_type", "")
                transformation_logic = mapping.get("transformation_logic", "")

                # Dedup key
                key = f"{target_table}|{source_column}|{target_column}"
                if key in seen_outgoing:
                    continue
                seen_outgoing.add(key)

                outgoing.append({
                    "target_table": target_table,
                    "target_table_qualified": target_table_qualified,
                    "source_table": source_table,
                    "source_table_qualified": source_table_qualified,
                    "source_column": source_column,
                    "target_column": target_column,
                    "transformation_type": transformation_type,
                    "transformation_logic": transformation_logic,
                })

        return {
            "incoming": incoming,
            "outgoing": outgoing,
        }

    def to_serialisable(self, subgraph: Optional[nx.DiGraph] = None) -> Dict[str, Any]:
        """
        Serialise graph (or subgraph) to a dictionary suitable for JSON.
        """
        if subgraph is None:
            subgraph = self.graph

        nodes = []
        for node_id in subgraph.nodes:
            node_data = subgraph.nodes[node_id]
            # Prefer qualified_name for label (set by catalogue enrichment),
            # then display_name (original name before normalisation), then node_id.
            display = (node_data.get("qualified_name")
                       or node_data.get("display_name")
                       or node_id)
            nodes.append({
                "id": node_id,
                "label": display,
                "display_name": node_data.get("display_name", node_id),
                "qualified_name": node_data.get("qualified_name", ""),
                "database": node_data.get("database", ""),
                "schema": node_data.get("schema", ""),
                "columns": node_data.get("columns", []),
                "column_details": node_data.get("column_details", []),
                "in_catalogue": node_data.get("in_catalogue", False),
            })

        edges = []
        for source, target in subgraph.edges:
            edge_data = subgraph.edges[source, target]
            edges.append({
                "source": source,
                "target": target,
                "column_mappings": edge_data.get("column_mappings", []),
                "joins": edge_data.get("joins", []),
                "filters": edge_data.get("filters", []),
                "grouping": edge_data.get("grouping", []),
                "procedures": edge_data.get("procedures", []),
                "extraction_methods": edge_data.get("extraction_methods", []),
            })

        return {
            "nodes": nodes,
            "edges": edges,
        }

    def statistics(self) -> Dict[str, Any]:
        """
        Return statistics about the lineage graph.
        """
        total_tables = self.graph.number_of_nodes()
        total_edges = self.graph.number_of_edges()

        # Count source tables (nodes with only outgoing edges, no incoming)
        source_tables = 0
        target_tables = 0
        for node in self.graph.nodes:
            in_degree = self.graph.in_degree(node)
            out_degree = self.graph.out_degree(node)
            if in_degree == 0 and out_degree > 0:
                source_tables += 1
            elif in_degree > 0 and out_degree == 0:
                target_tables += 1

        # Count unique procedures
        procedures = set()
        for _, _, edge_data in self.graph.edges(data=True):
            procedures.update(edge_data.get("procedures", []))

        # Count by extraction method
        method_counts = {"regex": 0, "claude": 0, "gemini": 0, "nvidia": 0, "failed": 0}
        for _, _, edge_data in self.graph.edges(data=True):
            for method in edge_data.get("extraction_methods", []):
                if method in method_counts:
                    method_counts[method] += 1

        return {
            "total_tables": total_tables,
            "total_edges": total_edges,
            "source_tables": source_tables,
            "target_tables": target_tables,
            "procedures": len(procedures),
            "by_method": method_counts,
        }


    def _build_reverse_mapping_index(self) -> Dict[str, List[Dict]]:
        """
        Build source_table.source_column -> list of (target_table, mapping) pairs.

        Deduplicates by (target_table, target_column) per key so that the same
        mapping copied onto multiple graph edges by the graph builder does not
        produce duplicate entries.
        """
        if hasattr(self, '_reverse_index'):
            return self._reverse_index

        index = {}
        for src, tgt, data in self.graph.edges(data=True):
            for m in data.get("column_mappings", []):
                st = normalise_table(m.get("source_table", "")) or src
                sc = m.get("source_column", "")
                key = f"{st}|{sc}"
                entry = {
                    "source_table": st,
                    "source_column": sc,
                    "target_table": tgt,
                    "target_column": m.get("target_column", ""),
                    "transformation_type": m.get("transformation_type", ""),
                    "transformation_logic": m.get("transformation_logic", ""),
                }
                # Dedup: skip if we already have this (target_table, target_column)
                # for the same source key
                existing = index.setdefault(key, [])
                dedup = f"{tgt}|{entry['target_column']}"
                if not any(f"{e['target_table']}|{e['target_column']}" == dedup for e in existing):
                    existing.append(entry)
        self._reverse_index = index
        return index

    def _is_raw_table(self, table_name: str) -> bool:
        """
        Returns True if the table is a "raw" source — meaning it has in_degree=0 (no predecessors),
        or its node's qualified_name/display_name contains "raw"/"Raw".
        """
        if self.graph.in_degree(table_name) == 0:
            return True
        node = self.graph.nodes[table_name]
        display_name = node.get("display_name", "")
        qualified_name = node.get("qualified_name", "")
        if "raw" in display_name.lower() or "raw" in qualified_name.lower():
            return True
        return False

    def trace_column_lineage(self, table: str, column: str) -> Dict[str, Any]:
        """
        Trace column lineage inbound (to raw/source) and outbound (to consumers).
        Returns a dict with keys:
          - inbound: list of chains (each chain is a list of steps from origin to the given column)
          - outbound: list of chains (each chain is a list of steps from the given column to leaf consumers)
          - tables_in_chain: set of table names involved in any chain (for highlighting)
        """
        table = normalise_table(table)
        if not self.graph.has_node(table):
            return {"inbound": [], "outbound": [], "tables_in_chain": []}

        MAX_DEPTH = 20

        def _trace_inbound(current_table: str, current_column: str, visited: set, depth: int) -> List[List[Dict]]:
            if depth > MAX_DEPTH:
                return [[{
                    "table": current_table,
                    "column": current_column,
                    "type": "cycle_detected",
                    "leaf": False,
                    "note": "⚠ cycle detected (max depth exceeded)"
                }]]
            key = f"{current_table}|{current_column}"
            if key in visited:
                return [[{
                    "table": current_table,
                    "column": current_column,
                    "type": "cycle_detected",
                    "leaf": False,
                    "note": "⚠ cycle detected"
                }]]
            visited = visited | {key}  # create a new set for this path

            chains = []
            seen_local: set = set()  # dedup same mapping across multiple predecessor edges
            for pred in self.graph.predecessors(current_table):
                edge_data = self.graph[pred][current_table]
                for mapping in edge_data.get("column_mappings", []):
                    if mapping.get("target_column") == current_column:
                        source_table = normalise_table(mapping.get("source_table", "")) or pred
                        source_column = mapping.get("source_column", "")
                        trans_type = mapping.get("transformation_type", "")

                        # Deduplicate: skip if we've already processed this exact
                        # (source_table, source_column, target_column) triple on
                        # another predecessor edge.  The graph builder copies an
                        # SP's full mapping list onto every edge, so the same
                        # mapping appears on multiple predecessor edges.
                        dedup_key = f"{source_table}|{source_column}|{current_column}"
                        if dedup_key in seen_local:
                            continue
                        seen_local.add(dedup_key)

                        if not source_table or trans_type == "constant":
                            chains.append([{
                                "table": current_table,
                                "column": current_column,
                                "type": trans_type if trans_type else "direct_copy",
                                "leaf": False
                            }, {
                                "table": source_table or "(constant)",
                                "column": source_column or "(constant)",
                                "type": trans_type if trans_type else "direct_copy",
                                "leaf": True,
                                "note": "constant" if not source_table else f"constant: {trans_type}"
                            }])
                        elif self._is_raw_table(source_table):
                            chains.append([{
                                "table": current_table,
                                "column": current_column,
                                "type": trans_type if trans_type else "direct_copy",
                                "leaf": False
                            }, {
                                "table": source_table,
                                "column": source_column,
                                "type": trans_type if trans_type else "direct_copy",
                                "leaf": True,
                                "note": "raw origin"
                            }])
                        else:
                            subchains = _trace_inbound(source_table, source_column, visited, depth + 1)
                            for subchain in subchains:
                                chains.append([{
                                    "table": current_table,
                                    "column": current_column,
                                    "type": trans_type if trans_type else "direct_copy",
                                    "leaf": False
                                }] + subchain)
            if not chains:
                chains = [[{
                    "table": current_table,
                    "column": current_column,
                    "type": "unknown",
                    "leaf": True,
                    "note": "no incoming mappings"
                }]]
            return chains

        def _trace_outbound(current_table: str, current_column: str, visited: set, depth: int) -> List[List[Dict]]:
            if depth > MAX_DEPTH:
                return [[{
                    "table": current_table,
                    "column": current_column,
                    "type": "cycle_detected",
                    "leaf": False,
                    "note": "⚠ cycle detected (max depth exceeded)"
                }]]
            key = f"{current_table}|{current_column}"
            if key in visited:
                return [[{
                    "table": current_table,
                    "column": current_column,
                    "type": "cycle_detected",
                    "leaf": False,
                    "note": "⚠ cycle detected"
                }]]
            visited = visited | {key}

            rev_index = self._build_reverse_mapping_index()
            key = f"{normalise_table(current_table)}|{current_column}"
            all_mappings = rev_index.get(key, [])

            # Deduplicate: the same (target_table, target_column) pair can appear
            # multiple times in the reverse index because the graph builder copies
            # an SP's full mapping list onto every edge.  Keep only unique
            # (target_table, target_column) combos to avoid producing the same
            # outbound branch multiple times.
            seen_local: set = set()
            mappings = []
            for m in all_mappings:
                dedup_key = f"{m['target_table']}|{m['target_column']}"
                if dedup_key not in seen_local:
                    seen_local.add(dedup_key)
                    mappings.append(m)

            chains = []
            if not mappings:
                chains = [[{
                    "table": current_table,
                    "column": current_column,
                    "type": "leaf",
                    "leaf": True,
                    "note": "Not consumed by any other table"
                }]]
            else:
                for mapping in mappings:
                    target_table = mapping["target_table"]
                    target_column = mapping["target_column"]
                    trans_type = mapping["transformation_type"]
                    subchains = _trace_outbound(target_table, target_column, visited, depth + 1)
                    for subchain in subchains:
                        chains.append([{
                            "table": current_table,
                            "column": current_column,
                            "type": trans_type if trans_type else "direct_copy",
                            "leaf": False
                        }] + subchain)
            return chains

        inbound = _trace_inbound(table, column, set(), 0)
        outbound = _trace_outbound(table, column, set(), 0)

        # Final safety-net dedup: remove byte-for-byte identical chains.
        # This preserves genuine fan-in (different source columns per branch)
        # but strips any residual exact duplicates the per-hop dedup missed.
        def _dedup_chains(chains):
            seen = set()
            result = []
            for chain in chains:
                sig = "|".join(f"{s['table']}.{s['column']}" for s in chain)
                if sig not in seen:
                    seen.add(sig)
                    result.append(chain)
            return result

        inbound = _dedup_chains(inbound)
        outbound = _dedup_chains(outbound)

        tables_in_chain = set()
        for chain in inbound + outbound:
            for step in chain:
                tbl = step["table"]
                if tbl not in ["(constant)", "(unknown)"]:
                    tables_in_chain.add(tbl)

        return {
            "inbound": inbound,
            "outbound": outbound,
            "tables_in_chain": list(tables_in_chain)
        }


if __name__ == "__main__":
    # For testing
    logging.basicConfig(level=logging.INFO)
    # Mock normalised SP results
    mock_results = [
        {
            "procedure_name": "sp_load_customer_summary",
            "target_table": "CUSTOMERS",
            "target_table_qualified": "[SalesDB].[dbo].[Customers]",
            "source_tables": ["ORDERS"],
            "source_tables_qualified": ["[SalesDB].[dbo].[Orders]"],
            "column_mappings": [
                {
                    "target_column": "CUSTOMER_ID",
                    "source_table": "ORDERS",
                    "source_table_qualified": "[SalesDB].[dbo].[Orders]",
                    "source_column": "CUSTOMER_ID",
                    "transformation_type": "direct_copy",
                    "transformation_logic": "Direct copy of customer ID",
                }
            ],
            "joins": [],
            "filters": [],
            "grouping": [],
            "extraction_method": "regex",
        }
    ]
    agent = LineageAgent(mock_results)
    print("All tables:", agent.all_tables())
    print("Statistics:", agent.statistics())