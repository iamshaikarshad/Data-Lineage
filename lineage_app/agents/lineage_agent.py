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
        """
        logger.info("Building lineage graph from %d SP results", len(results))

        for result in results:
            target = result.get("target_table")
            if not target:
                continue

            # Ensure target node exists
            self._ensure_node(target, result)

            # Process source tables and create edges
            source_tables = result.get("source_tables", [])
            for source in source_tables:
                if not source:
                    continue

                # Ensure source node exists
                self._ensure_node(source, result)

                # Add or update edge from source to target
                self._add_or_update_edge(source, target, result)

    def _ensure_node(self, table_name: str, result: Dict[str, Any]) -> None:
        """
        Ensure a node exists in the graph, creating it if necessary.
        """
        if self.graph.has_node(table_name):
            # Node already exists, we might want to update some attributes
            # For now, we'll keep the existing node as is
            return

        # Create new node with default attributes
        self.graph.add_node(
            table_name,
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
        """
        table = normalise_table(table)
        if not self.graph.has_node(table):
            return {"incoming": [], "outgoing": []}

        incoming = []
        outgoing = []

        # Get incoming edges (predecessors)
        for predecessor in self.graph.predecessors(table):
            edge_data = self.graph[predecessor][table]
            for mapping in edge_data.get("column_mappings", []):
                incoming.append({
                    "source_table": predecessor,
                    "source_table_qualified": mapping.get("source_table_qualified", ""),
                    "source_column": mapping.get("source_column", ""),
                    "target_column": mapping.get("target_column", ""),
                    "transformation_type": mapping.get("transformation_type", ""),
                    "transformation_logic": mapping.get("transformation_logic", ""),
                })

        # Get outgoing edges (successors)
        for successor in self.graph.successors(table):
            edge_data = self.graph[table][successor]
            for mapping in edge_data.get("column_mappings", []):
                outgoing.append({
                    "target_table": successor,
                    "target_table_qualified": mapping.get("target_table_qualified", ""),
                    "source_column": mapping.get("source_column", ""),
                    "target_column": mapping.get("target_column", ""),
                    "transformation_type": mapping.get("transformation_type", ""),
                    "transformation_logic": mapping.get("transformation_logic", ""),
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
            nodes.append({
                "id": node_id,
                "label": node_id,
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