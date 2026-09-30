import { createEffect, createSignal, onCleanup, onMount } from "solid-js";
import cytoscape from "cytoscape";
import type { RecordData } from "./api";

export default function Graph(props: {
  topology: { nodes: RecordData[]; edges: RecordData[] };
  select: (node: RecordData) => void;
  selectedId?: string;
}) {
  const [layer, setLayer] = createSignal("all");
  const [collapsed, setCollapsed] = createSignal(false);
  let element!: HTMLDivElement;
  let graph: cytoscape.Core | undefined;
  let resizeObserver: ResizeObserver | undefined;
  onMount(() => {
    graph = cytoscape({
      container: element,
      elements: [],
      layout: { name: "preset" },
      wheelSensitivity: 0.25,
      minZoom: 0.2,
      maxZoom: 3,
      textureOnViewport: true,
      pixelRatio: 1,
      style: [
        {
          selector: "node",
          style: {
            label: "data(label)",
            "background-color": "#88dfcc",
            color: "#e3e0d4",
            "font-family": "monospace",
            "font-size": 10,
            "text-valign": "bottom",
            "text-margin-y": 12,
            width: 24,
            height: 24,
            shape: "ellipse",
          },
        },
        {
          selector: "node[kind = 'device']",
          style: {
            width: 42,
            height: 42,
            "background-color": "#1b242b",
            "border-color": "#88dfcc",
            "border-width": 2,
          },
        },
        {
          selector: "edge",
          style: {
            width: 1,
            "line-color": "#647671",
            "target-arrow-color": "#647671",
            "target-arrow-shape": "triangle",
            "curve-style": "bezier",
          },
        },
        {
          selector: "[provenance = 'configured']",
          style: { "line-style": "dashed", opacity: 0.55 },
        },
        {
          selector: "[status = 'lost']",
          style: { "background-color": "#ef817c", "border-color": "#ef817c" },
        },
        {
          selector: "edge:selected",
          style: { "line-color": "#e4b46a", width: 2 },
        },
        {
          selector: ":selected",
          style: { "border-width": 3, "border-color": "#e4b46a" },
        },
      ],
    });
    graph.on("tap", "node", (event) => props.select(event.target.data()));
    update();
    // Keep dimensions and pointer coordinates current as the console resizes.
    resizeObserver = new ResizeObserver(() => graph?.resize());
    resizeObserver.observe(element);
  });
  function update() {
    if (!graph) return;
    const cy = graph;
    const nodes = props.topology.nodes,
      edges = props.topology.edges;
    const wanted = new Set([...nodes, ...edges].map((item) => item.id));
    cy.batch(() => {
      cy.elements()
        .filter((item) => !wanted.has(item.id()))
        .remove();
      nodes.forEach((node, i) => {
        const existing = cy.getElementById(node.id);
        if (existing.length) existing.data(node);
        else
          cy.add({
            data: node as cytoscape.NodeDataDefinition,
            position: { x: node.kind === "device" ? 100 : 340, y: 80 + i * 95 },
          });
      });
      edges.forEach((edge) => {
        const existing = cy.getElementById(edge.id);
        if (existing.length) existing.data(edge);
        else cy.add({ data: edge as cytoscape.EdgeDataDefinition });
      });
      cy.nodes().forEach((item) => {
        const show =
          (layer() === "all" || item.data("kind") === layer()) &&
          !(collapsed() && item.data("kind") === "capability");
        item.style("display", show ? "element" : "none");
      });
      if (props.selectedId) {
        cy.elements().unselect();
        const selected = cy.getElementById(props.selectedId);
        selected.select();
        selected.connectedEdges().select();
      }
    });
  }
  createEffect(update);
  onCleanup(() => {
    resizeObserver?.disconnect();
    graph?.destroy();
  });
  return (
    <div class="graph-shell">
      <div class="graph-tools">
        <label>
          Layer
          <select
            value={layer()}
            onChange={(e) => setLayer(e.currentTarget.value)}
          >
            <option value="all">All layers</option>
            <option value="device">Devices</option>
            <option value="capability">Capabilities</option>
          </select>
        </label>
        <button class="small" onClick={() => setCollapsed(!collapsed())}>
          {collapsed() ? "Expand capabilities" : "Collapse capabilities"}
        </button>
      </div>
      <div
        ref={element}
        class="graph"
        role="img"
        aria-label="Body topology. All graph information is also available in the resource table below."
      />
      <button class="graph-fit small" onClick={() => graph?.fit(undefined, 55)}>
        Fit view
      </button>
    </div>
  );
}
