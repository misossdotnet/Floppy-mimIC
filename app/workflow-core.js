(function initMimicFlowCore(globalScope) {
  "use strict";

  const NODE_CATALOG = Object.freeze({
    need: {
      label: "Besoin métier",
      shortLabel: "Besoin",
      category: "Entrées",
      icon: "B",
      tone: "blue",
      outputType: "source",
      accepts: [],
      description: "Point de départ saisi par un utilisateur."
    },
    document: {
      label: "Document source",
      shortLabel: "Document",
      category: "Entrées",
      icon: "D",
      tone: "blue",
      outputType: "source",
      accepts: [],
      description: "Document importé et contrôlé avant analyse."
    },
    analysis: {
      label: "Analyse IA",
      shortLabel: "Analyse IA",
      category: "Intelligence",
      icon: "IA",
      tone: "purple",
      outputType: "proposal",
      accepts: ["source"],
      description: "Structure une source en propositions non validées."
    },
    repository: {
      label: "Référentiel projet",
      shortLabel: "Référentiel",
      category: "Référentiel",
      icon: "R",
      tone: "teal",
      outputType: "context",
      accepts: ["source", "proposal", "context"],
      description: "Centralise besoins, règles, acteurs, risques et relations."
    },
    approval: {
      label: "Validation humaine",
      shortLabel: "Validation",
      category: "Gouvernance",
      icon: "H",
      tone: "amber",
      outputType: "approved",
      accepts: ["proposal", "context", "artifact"],
      description: "Suspend le flux jusqu'à la décision d'un rôle habilité."
    },
    generation: {
      label: "Génération de livrable",
      shortLabel: "Génération",
      category: "Intelligence",
      icon: "IA",
      tone: "purple",
      outputType: "artifact",
      accepts: ["context", "approved"],
      description: "Crée toujours une nouvelle version en Brouillon IA."
    },
    review: {
      label: "Revue de livrable",
      shortLabel: "Revue",
      category: "Gouvernance",
      icon: "H",
      tone: "amber",
      outputType: "approved",
      accepts: ["artifact"],
      description: "Collecte commentaires, validation ou rejet humain."
    },
    publication: {
      label: "Publication / export",
      shortLabel: "Publication",
      category: "Sorties",
      icon: "P",
      tone: "green",
      outputType: null,
      accepts: ["approved"],
      description: "Publie uniquement un artefact validé."
    }
  });

  const STATUS_LABELS = Object.freeze({
    ai_draft: "Brouillon IA",
    review: "À revoir",
    validated: "Validé",
    rejected: "Rejeté",
    published: "Publié",
    obsolete: "Obsolète",
    blocked: "Bloqué"
  });

  const GRAPH_STATUS_LABELS = Object.freeze({
    draft: "Brouillon",
    review: "À revoir",
    validated: "Validé",
    obsolete: "Obsolète"
  });

  function deepClone(value) {
    return JSON.parse(JSON.stringify(value));
  }

  function nodeById(graph, nodeId) {
    return graph.nodes.find((node) => node.id === nodeId);
  }

  function uniqueId(prefix, existingIds) {
    let index = existingIds.length + 1;
    let candidate = `${prefix}-${index}`;
    while (existingIds.includes(candidate)) {
      index += 1;
      candidate = `${prefix}-${index}`;
    }
    return candidate;
  }

  function createNode(kind, position, existingIds) {
    const definition = NODE_CATALOG[kind];
    if (!definition) {
      throw new Error(`Type de nœud inconnu : ${kind}`);
    }

    const id = uniqueId(kind, existingIds || []);
    const aiProduced = kind === "analysis" || kind === "generation";
    const initiallyBlocked = kind === "publication";

    return {
      id,
      kind,
      label: definition.label,
      description: definition.description,
      ownerRole: kind === "approval" || kind === "review"
        ? "Product Owner"
        : "Architecte Applicatif",
      status: initiallyBlocked ? "blocked" : (aiProduced ? "ai_draft" : "review"),
      x: Math.max(24, Math.round(position.x)),
      y: Math.max(24, Math.round(position.y))
    };
  }

  function createInitialGraph() {
    return {
      schemaVersion: 1,
      id: "flow-crm-cadrage",
      name: "Cadrage CRM — du besoin à la publication",
      description: "Chaîne de qualification des besoins, consolidation du référentiel et génération contrôlée.",
      status: "draft",
      version: 3,
      basedOnVersion: 2,
      updatedAt: "2026-07-29T08:42:00.000Z",
      nodes: [
        {
          id: "need-1",
          kind: "need",
          label: "Besoin CRM unifié",
          description: "Centraliser les besoins des équipes commerciales.",
          ownerRole: "Product Owner",
          status: "validated",
          x: 44,
          y: 104
        },
        {
          id: "analysis-1",
          kind: "analysis",
          label: "Structurer les besoins",
          description: "Extraire exigences, acteurs, règles et risques.",
          ownerRole: "Architecte Applicatif",
          status: "ai_draft",
          x: 316,
          y: 104
        },
        {
          id: "repository-1",
          kind: "repository",
          label: "Référentiel CRM",
          description: "Consolider les propositions avec les éléments validés.",
          ownerRole: "Product Owner",
          status: "review",
          x: 588,
          y: 104
        },
        {
          id: "approval-1",
          kind: "approval",
          label: "Validation du référentiel",
          description: "Décision humaine avant génération documentaire.",
          ownerRole: "Product Owner",
          status: "review",
          x: 860,
          y: 104
        },
        {
          id: "generation-1",
          kind: "generation",
          label: "Générer les user stories",
          description: "Créer une version traçable depuis le contexte validé.",
          ownerRole: "Architecte Applicatif",
          status: "blocked",
          x: 860,
          y: 354
        },
        {
          id: "review-1",
          kind: "review",
          label: "Revue Product Owner",
          description: "Commenter, corriger puis valider ou rejeter.",
          ownerRole: "Product Owner",
          status: "blocked",
          x: 1132,
          y: 354
        },
        {
          id: "publication-1",
          kind: "publication",
          label: "Publier la référence",
          description: "Exporter uniquement la version validée.",
          ownerRole: "Chef de Projet",
          status: "blocked",
          x: 1132,
          y: 604
        }
      ],
      edges: [
        { id: "edge-1", source: "need-1", target: "analysis-1" },
        { id: "edge-2", source: "analysis-1", target: "repository-1" },
        { id: "edge-3", source: "repository-1", target: "approval-1" },
        { id: "edge-4", source: "approval-1", target: "generation-1" },
        { id: "edge-5", source: "generation-1", target: "review-1" },
        { id: "edge-6", source: "review-1", target: "publication-1" }
      ]
    };
  }

  function graphHasCycle(nodes, edges) {
    const adjacency = new Map(nodes.map((node) => [node.id, []]));
    edges.forEach((edge) => {
      if (adjacency.has(edge.source)) {
        adjacency.get(edge.source).push(edge.target);
      }
    });

    const visiting = new Set();
    const visited = new Set();

    function visit(nodeId) {
      if (visiting.has(nodeId)) {
        return true;
      }
      if (visited.has(nodeId)) {
        return false;
      }

      visiting.add(nodeId);
      const cycleFound = (adjacency.get(nodeId) || []).some(visit);
      visiting.delete(nodeId);
      visited.add(nodeId);
      return cycleFound;
    }

    return nodes.some((node) => visit(node.id));
  }

  function checkConnection(graph, sourceId, targetId) {
    const source = nodeById(graph, sourceId);
    const target = nodeById(graph, targetId);

    if (!source || !target) {
      return { valid: false, reason: "Un des nœuds n'existe plus." };
    }
    if (sourceId === targetId) {
      return { valid: false, reason: "Un nœud ne peut pas être relié à lui-même." };
    }
    if (graph.edges.some((edge) => edge.source === sourceId && edge.target === targetId)) {
      return { valid: false, reason: "Cette connexion existe déjà." };
    }

    const sourceDefinition = NODE_CATALOG[source.kind];
    const targetDefinition = NODE_CATALOG[target.kind];
    if (!sourceDefinition || !targetDefinition) {
      return { valid: false, reason: "Un des types de nœuds n'est pas enregistré dans le catalogue." };
    }
    if (!sourceDefinition.outputType) {
      return { valid: false, reason: `${sourceDefinition.label} ne produit pas de sortie.` };
    }
    if (!targetDefinition.accepts.includes(sourceDefinition.outputType)) {
      return {
        valid: false,
        reason: `${targetDefinition.label} n'accepte pas une sortie « ${sourceDefinition.outputType} ».`
      };
    }

    const candidateEdges = graph.edges.concat({
      id: "candidate",
      source: sourceId,
      target: targetId
    });
    if (graphHasCycle(graph.nodes, candidateEdges)) {
      return { valid: false, reason: "Cette connexion créerait une boucle non autorisée." };
    }

    return { valid: true, reason: "" };
  }

  function hasHumanGateUpstream(graph, nodeId) {
    const incomingByTarget = new Map();
    graph.edges.forEach((edge) => {
      const incoming = incomingByTarget.get(edge.target) || [];
      incoming.push(edge.source);
      incomingByTarget.set(edge.target, incoming);
    });

    const visited = new Set();
    const queue = [...(incomingByTarget.get(nodeId) || [])];
    while (queue.length) {
      const currentId = queue.shift();
      if (visited.has(currentId)) {
        continue;
      }
      visited.add(currentId);
      const current = nodeById(graph, currentId);
      if (current && (current.kind === "approval" || current.kind === "review")) {
        return true;
      }
      queue.push(...(incomingByTarget.get(currentId) || []));
    }
    return false;
  }

  function validateGraph(graph) {
    const errors = [];
    const warnings = [];
    const nodeIds = new Set(graph.nodes.map((node) => node.id));
    const edgePairs = new Set();

    if (!graph.name || !graph.name.trim()) {
      errors.push("Le flux doit avoir un nom.");
    }
    if (!graph.nodes.length) {
      errors.push("Le flux doit contenir au moins un nœud.");
    }
    graph.nodes.forEach((node) => {
      if (!NODE_CATALOG[node.kind]) {
        errors.push(`Le type du nœud « ${node.id} » n'est pas enregistré dans le catalogue.`);
      }
    });

    graph.edges.forEach((edge) => {
      if (!nodeIds.has(edge.source) || !nodeIds.has(edge.target)) {
        errors.push(`La connexion ${edge.id} référence un nœud absent.`);
        return;
      }
      const source = nodeById(graph, edge.source);
      const target = nodeById(graph, edge.target);
      const sourceDefinition = NODE_CATALOG[source.kind];
      const targetDefinition = NODE_CATALOG[target.kind];
      if (!sourceDefinition || !targetDefinition) {
        return;
      }
      const pair = `${edge.source}->${edge.target}`;
      if (edgePairs.has(pair)) {
        errors.push(`La connexion entre « ${source.label} » et « ${target.label} » est dupliquée.`);
      }
      edgePairs.add(pair);
      const outputType = sourceDefinition.outputType;
      if (!targetDefinition.accepts.includes(outputType)) {
        errors.push(`Connexion incompatible entre « ${source.label} » et « ${target.label} ».`);
      }
    });

    if (graphHasCycle(graph.nodes, graph.edges)) {
      errors.push("Le flux contient une boucle non autorisée.");
    }

    graph.nodes.forEach((node) => {
      const definition = NODE_CATALOG[node.kind];
      if (!definition) {
        return;
      }
      const incoming = graph.edges.filter((edge) => edge.target === node.id);
      const outgoing = graph.edges.filter((edge) => edge.source === node.id);

      if (definition.accepts.length && !incoming.length) {
        warnings.push(`« ${node.label} » n'a aucune entrée.`);
      }
      if (definition.outputType && !outgoing.length) {
        warnings.push(`« ${node.label} » n'est relié à aucune étape suivante.`);
      }
      if ((node.kind === "analysis" || node.kind === "generation") && node.status === "validated") {
        errors.push(`« ${node.label} » est une étape IA et ne peut pas s'auto-déclarer validée.`);
      }
      if (node.kind === "publication" && !hasHumanGateUpstream(graph, node.id)) {
        errors.push(`« ${node.label} » doit avoir une validation humaine en amont.`);
      }
    });

    if (!graph.nodes.some((node) => node.kind === "need" || node.kind === "document")) {
      warnings.push("Le flux ne possède aucune source métier explicite.");
    }
    if (!graph.nodes.some((node) => node.kind === "approval" || node.kind === "review")) {
      errors.push("Le flux doit inclure au moins une décision humaine.");
    }

    return { valid: errors.length === 0, errors, warnings };
  }

  function createSnapshot(graph, actor, timestamp) {
    return {
      id: `${graph.id}-v${graph.version}`,
      flowId: graph.id,
      version: graph.version,
      status: graph.status,
      actor,
      createdAt: timestamp || new Date().toISOString(),
      content: deepClone(graph)
    };
  }

  const api = {
    NODE_CATALOG,
    STATUS_LABELS,
    GRAPH_STATUS_LABELS,
    checkConnection,
    createInitialGraph,
    createNode,
    createSnapshot,
    deepClone,
    graphHasCycle,
    hasHumanGateUpstream,
    validateGraph
  };

  if (typeof module !== "undefined" && module.exports) {
    module.exports = api;
  }
  globalScope.MimicFlowCore = api;
})(typeof window !== "undefined" ? window : globalThis);
