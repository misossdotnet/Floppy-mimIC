"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const core = require("./workflow-core.js");

test("le flux de démonstration est structurellement valide", () => {
  const graph = core.createInitialGraph();
  const result = core.validateGraph(graph);

  assert.equal(result.valid, true);
  assert.deepEqual(result.errors, []);
});

test("une connexion compatible est acceptée", () => {
  const graph = core.createInitialGraph();
  graph.edges = graph.edges.filter((edge) => edge.source !== "need-1");

  const result = core.checkConnection(graph, "need-1", "analysis-1");

  assert.equal(result.valid, true);
});

test("un port incompatible est refusé", () => {
  const graph = core.createInitialGraph();

  const result = core.checkConnection(graph, "need-1", "publication-1");

  assert.equal(result.valid, false);
  assert.match(result.reason, /n'accepte pas/);
});

test("un type de nœud absent du catalogue est refusé sans erreur technique", () => {
  const graph = core.createInitialGraph();
  graph.nodes[0].kind = "script_arbitraire";

  const result = core.validateGraph(graph);

  assert.equal(result.valid, false);
  assert.ok(result.errors.some((message) => message.includes("pas enregistré")));
});

test("une connexion dupliquée invalide le graphe", () => {
  const graph = core.createInitialGraph();
  graph.edges.push({ id: "edge-duplicate", source: "need-1", target: "analysis-1" });

  const result = core.validateGraph(graph);

  assert.equal(result.valid, false);
  assert.ok(result.errors.some((message) => message.includes("dupliquée")));
});

test("une connexion qui créerait un cycle est refusée", () => {
  const graph = core.createInitialGraph();

  const result = core.checkConnection(graph, "review-1", "generation-1");

  assert.equal(result.valid, false);
  assert.match(result.reason, /boucle/);
});

test("une étape IA ne peut pas se déclarer validée", () => {
  const graph = core.createInitialGraph();
  graph.nodes.find((node) => node.id === "analysis-1").status = "validated";

  const result = core.validateGraph(graph);

  assert.equal(result.valid, false);
  assert.ok(result.errors.some((message) => message.includes("ne peut pas s'auto-déclarer validée")));
});

test("une publication exige une porte humaine en amont", () => {
  const graph = core.createInitialGraph();
  graph.nodes = graph.nodes.filter((node) => node.kind !== "approval" && node.kind !== "review");
  graph.edges = graph.edges.filter((edge) => (
    graph.nodes.some((node) => node.id === edge.source)
    && graph.nodes.some((node) => node.id === edge.target)
  ));

  const result = core.validateGraph(graph);

  assert.equal(result.valid, false);
  assert.ok(result.errors.some((message) => message.includes("validation humaine")));
});

test("un snapshot ne partage pas les objets mutables du graphe", () => {
  const graph = core.createInitialGraph();
  const snapshot = core.createSnapshot(graph, "Architecte Applicatif", "2026-07-29T10:00:00.000Z");

  graph.nodes[0].label = "Libellé modifié";

  assert.equal(snapshot.content.nodes[0].label, "Besoin CRM unifié");
  assert.equal(snapshot.version, 3);
});
