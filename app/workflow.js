(function initWorkflowPrototype() {
  "use strict";

  const core = window.MimicFlowCore;
  const STORAGE_KEY = "floppy-mimic.workflow.prototype.v1";
  const NODE_WIDTH = 220;
  const PORT_Y = 74;

  const elements = {
    stage: document.querySelector("#flow-stage"),
    viewport: document.querySelector("#flow-viewport"),
    nodes: document.querySelector("#flow-nodes"),
    edges: document.querySelector("#flow-edges"),
    palette: document.querySelector("#node-palette"),
    inspector: document.querySelector("#node-inspector"),
    inspectorEmpty: document.querySelector("#inspector-empty"),
    nodeForm: document.querySelector("#node-form"),
    nodeLabel: document.querySelector("#node-label"),
    nodeDescription: document.querySelector("#node-description"),
    nodeOwner: document.querySelector("#node-owner"),
    nodeStatus: document.querySelector("#node-status"),
    nodeType: document.querySelector("#node-type"),
    deleteNode: document.querySelector("#delete-node"),
    graphName: document.querySelector("#graph-name"),
    graphStatus: document.querySelector("#graph-status"),
    graphVersion: document.querySelector("#graph-version"),
    graphUpdated: document.querySelector("#graph-updated"),
    dirtyState: document.querySelector("#dirty-state"),
    connectionHint: document.querySelector("#connection-hint"),
    validationSummary: document.querySelector("#validation-summary"),
    runLog: document.querySelector("#run-log"),
    save: document.querySelector("#save-flow"),
    submitReview: document.querySelector("#submit-review"),
    validate: document.querySelector("#validate-flow"),
    test: document.querySelector("#test-flow"),
    execute: document.querySelector("#execute-flow"),
    reset: document.querySelector("#reset-flow"),
    fit: document.querySelector("#fit-flow"),
    zoomIn: document.querySelector("#zoom-in"),
    zoomOut: document.querySelector("#zoom-out"),
    toast: document.querySelector("#flow-toast")
  };

  let graph = loadGraph();
  let snapshots = loadSnapshots();
  let selectedNodeId = graph.nodes[0] ? graph.nodes[0].id : null;
  let pendingSourceId = null;
  let dirty = false;
  let zoom = 1;
  let toastTimer;

  function loadStoredState() {
    try {
      const raw = window.localStorage.getItem(STORAGE_KEY);
      return raw ? JSON.parse(raw) : null;
    } catch (error) {
      return null;
    }
  }

  function loadGraph() {
    const stored = loadStoredState();
    if (stored && stored.graph && stored.graph.schemaVersion === 1) {
      return stored.graph;
    }
    return core.createInitialGraph();
  }

  function loadSnapshots() {
    const stored = loadStoredState();
    return stored && Array.isArray(stored.snapshots) ? stored.snapshots : [];
  }

  function persistState() {
    window.localStorage.setItem(STORAGE_KEY, JSON.stringify({ graph, snapshots }));
  }

  function escapeHtml(value) {
    return String(value)
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;")
      .replaceAll("'", "&#039;");
  }

  function showToast(message, tone) {
    window.clearTimeout(toastTimer);
    elements.toast.textContent = message;
    elements.toast.className = `flow-toast visible ${tone || "info"}`;
    toastTimer = window.setTimeout(() => {
      elements.toast.className = "flow-toast";
    }, 3600);
  }

  function statusClass(status) {
    if (status === "validated" || status === "published") return "validated";
    if (status === "ai_draft") return "ai";
    if (status === "review") return "review";
    if (status === "rejected" || status === "blocked") return "rejected";
    return "obsolete";
  }

  function setDirty() {
    if (!dirty) {
      graph.basedOnVersion = graph.version;
      graph.version += 1;
    }
    graph.status = "draft";
    graph.updatedAt = new Date().toISOString();
    dirty = true;
    renderHeader();
  }

  function renderHeader() {
    elements.graphName.value = graph.name;
    elements.graphStatus.textContent = core.GRAPH_STATUS_LABELS[graph.status] || graph.status;
    elements.graphStatus.className = `status ${graph.status === "validated" ? "validated" : (graph.status === "review" ? "review" : "ai")}`;
    elements.graphVersion.textContent = `v${graph.version}`;
    elements.graphUpdated.textContent = new Intl.DateTimeFormat("fr-FR", {
      dateStyle: "medium",
      timeStyle: "short"
    }).format(new Date(graph.updatedAt));
    elements.dirtyState.textContent = dirty ? "Modifications non enregistrées" : "Version enregistrée";
    elements.dirtyState.className = `save-state ${dirty ? "dirty" : "saved"}`;
    elements.validate.disabled = graph.status !== "review";
    elements.execute.disabled = graph.status !== "validated";
  }

  function renderPalette() {
    const grouped = Object.entries(core.NODE_CATALOG).reduce((result, [kind, definition]) => {
      result[definition.category] = result[definition.category] || [];
      result[definition.category].push({ kind, ...definition });
      return result;
    }, {});

    elements.palette.innerHTML = Object.entries(grouped).map(([category, definitions]) => `
      <section class="palette-group">
        <h3>${escapeHtml(category)}</h3>
        ${definitions.map((definition) => `
          <button class="palette-item" type="button" data-add-kind="${definition.kind}">
            <span class="node-icon ${definition.tone}">${definition.icon}</span>
            <span>
              <strong>${escapeHtml(definition.shortLabel)}</strong>
              <small>${escapeHtml(definition.description)}</small>
            </span>
            <span aria-hidden="true" class="palette-add">+</span>
          </button>
        `).join("")}
      </section>
    `).join("");
  }

  function renderEdges() {
    const definitions = `
      <defs>
        <marker id="edge-arrow" viewBox="0 0 10 10" refX="8" refY="5" markerWidth="6" markerHeight="6" orient="auto-start-reverse">
          <path d="M 0 0 L 10 5 L 0 10 z"></path>
        </marker>
      </defs>
    `;
    elements.edges.innerHTML = definitions + graph.edges.map((edge) => {
      const source = graph.nodes.find((node) => node.id === edge.source);
      const target = graph.nodes.find((node) => node.id === edge.target);
      if (!source || !target) return "";

      const x1 = source.x + NODE_WIDTH;
      const y1 = source.y + PORT_Y;
      const x2 = target.x;
      const y2 = target.y + PORT_Y;
      const bend = Math.max(70, Math.abs(x2 - x1) * 0.42);
      const path = `M ${x1} ${y1} C ${x1 + bend} ${y1}, ${x2 - bend} ${y2}, ${x2} ${y2}`;
      return `
        <g class="flow-edge" data-edge-id="${edge.id}">
          <path class="edge-hit" d="${path}"></path>
          <path class="edge-line" d="${path}" marker-end="url(#edge-arrow)"></path>
        </g>
      `;
    }).join("");
  }

  function renderNodes() {
    elements.nodes.innerHTML = graph.nodes.map((node) => {
      const definition = core.NODE_CATALOG[node.kind];
      const selected = node.id === selectedNodeId;
      const pending = node.id === pendingSourceId;
      return `
        <article
          class="flow-node ${selected ? "selected" : ""} ${pending ? "connecting" : ""}"
          data-node-id="${node.id}"
          tabindex="0"
          aria-label="${escapeHtml(definition.label)} : ${escapeHtml(node.label)}"
          style="left:${node.x}px;top:${node.y}px"
        >
          ${definition.accepts.length ? `
            <button class="node-port input-port" type="button" data-port="input" aria-label="Connecter une entrée à ${escapeHtml(node.label)}"></button>
          ` : ""}
          <header class="flow-node-header">
            <span class="node-icon ${definition.tone}">${definition.icon}</span>
            <span class="node-kind">${escapeHtml(definition.shortLabel)}</span>
            <button class="node-menu" type="button" data-node-menu aria-label="Sélectionner ${escapeHtml(node.label)}">•••</button>
          </header>
          <div class="flow-node-body">
            <strong>${escapeHtml(node.label)}</strong>
            <p>${escapeHtml(node.description)}</p>
          </div>
          <footer class="flow-node-footer">
            <span class="status ${statusClass(node.status)}">${escapeHtml(core.STATUS_LABELS[node.status] || node.status)}</span>
            <small>${escapeHtml(node.ownerRole)}</small>
          </footer>
          ${definition.outputType ? `
            <button class="node-port output-port" type="button" data-port="output" aria-label="Relier la sortie de ${escapeHtml(node.label)}"></button>
          ` : ""}
        </article>
      `;
    }).join("");
  }

  function renderInspector() {
    const selected = graph.nodes.find((node) => node.id === selectedNodeId);
    elements.inspectorEmpty.hidden = Boolean(selected);
    elements.nodeForm.hidden = !selected;
    if (!selected) return;

    elements.nodeType.textContent = core.NODE_CATALOG[selected.kind].label;
    elements.nodeLabel.value = selected.label;
    elements.nodeDescription.value = selected.description;
    elements.nodeOwner.value = selected.ownerRole;
    elements.nodeStatus.value = selected.status;
    elements.deleteNode.disabled = graph.nodes.length <= 1;
  }

  function renderConnectionHint() {
    if (!pendingSourceId) {
      elements.connectionHint.innerHTML = "<strong>Connexion</strong><span>Cliquez sur le port de sortie d’un nœud, puis sur une entrée compatible.</span>";
      return;
    }
    const source = graph.nodes.find((node) => node.id === pendingSourceId);
    elements.connectionHint.innerHTML = `
      <strong>Connexion en cours</strong>
      <span>Choisissez l’entrée cible pour « ${escapeHtml(source.label)} » ou appuyez sur Échap.</span>
    `;
  }

  function renderValidation() {
    const result = core.validateGraph(graph);
    const tone = result.errors.length ? "error" : (result.warnings.length ? "warning" : "success");
    const headline = result.errors.length
      ? `${result.errors.length} erreur${result.errors.length > 1 ? "s" : ""} bloquante${result.errors.length > 1 ? "s" : ""}`
      : (result.warnings.length ? `${result.warnings.length} point${result.warnings.length > 1 ? "s" : ""} à vérifier` : "Flux structurellement valide");
    const messages = result.errors.concat(result.warnings).slice(0, 4);

    elements.validationSummary.className = `validation-summary ${tone}`;
    elements.validationSummary.innerHTML = `
      <div><span class="validation-dot"></span><strong>${headline}</strong></div>
      ${messages.length ? `<ul>${messages.map((message) => `<li>${escapeHtml(message)}</li>`).join("")}</ul>` : "<p>Ports compatibles, graphe acyclique et validation humaine présente.</p>"}
    `;
  }

  function render() {
    renderHeader();
    renderNodes();
    renderEdges();
    renderInspector();
    renderConnectionHint();
    renderValidation();
    elements.stage.style.transform = `scale(${zoom})`;
    elements.stage.style.transformOrigin = "top left";
  }

  function addNode(kind) {
    const position = {
      x: elements.viewport.scrollLeft + 180 + (graph.nodes.length % 3) * 32,
      y: elements.viewport.scrollTop + 140 + (graph.nodes.length % 4) * 42
    };
    const node = core.createNode(kind, position, graph.nodes.map((item) => item.id));
    graph.nodes.push(node);
    selectedNodeId = node.id;
    setDirty();
    render();
    showToast(`Nœud « ${node.label} » ajouté.`, "success");
  }

  function removeSelectedNode() {
    if (!selectedNodeId || graph.nodes.length <= 1) return;
    const selected = graph.nodes.find((node) => node.id === selectedNodeId);
    graph.nodes = graph.nodes.filter((node) => node.id !== selectedNodeId);
    graph.edges = graph.edges.filter((edge) => edge.source !== selectedNodeId && edge.target !== selectedNodeId);
    selectedNodeId = graph.nodes[0] ? graph.nodes[0].id : null;
    pendingSourceId = null;
    setDirty();
    render();
    showToast(`Nœud « ${selected.label} » retiré du brouillon.`, "info");
  }

  function connectNodes(targetId) {
    if (!pendingSourceId) return;
    const result = core.checkConnection(graph, pendingSourceId, targetId);
    if (!result.valid) {
      showToast(result.reason, "error");
      return;
    }

    graph.edges.push({
      id: `edge-${Date.now()}`,
      source: pendingSourceId,
      target: targetId
    });
    pendingSourceId = null;
    setDirty();
    render();
    showToast("Connexion ajoutée.", "success");
  }

  function saveGraph(nextStatus) {
    const validation = core.validateGraph(graph);
    if (nextStatus === "review" && !validation.valid) {
      showToast("Corrigez les erreurs bloquantes avant la mise en revue.", "error");
      renderValidation();
      return false;
    }

    const contentChanged = dirty;
    graph.status = nextStatus || graph.status;
    graph.updatedAt = new Date().toISOString();
    if (contentChanged || !snapshots.some((snapshot) => snapshot.version === graph.version)) {
      snapshots.push(core.createSnapshot(graph, "Architecte Applicatif", graph.updatedAt));
    }
    snapshots = snapshots.slice(-20);
    dirty = false;
    persistState();
    render();
    return true;
  }

  function runDryTest() {
    const validation = core.validateGraph(graph);
    const now = new Intl.DateTimeFormat("fr-FR", { timeStyle: "medium" }).format(new Date());
    const rows = [
      `[${now}] Test à blanc — aucun service externe appelé`,
      `[${now}] ${graph.nodes.length} nœuds, ${graph.edges.length} connexions`,
      `[${now}] Contrôle des ports : ${validation.errors.length ? "ÉCHEC" : "OK"}`,
      `[${now}] Contrôle Human-in-the-Loop : ${graph.nodes.some((node) => node.kind === "approval" || node.kind === "review") ? "OK" : "ÉCHEC"}`,
      `[${now}] Résultat : ${validation.valid ? "flux prêt pour revue humaine" : "flux bloqué"}`
    ];
    elements.runLog.innerHTML = rows.map((row, index) => `<li class="${index === rows.length - 1 ? (validation.valid ? "log-success" : "log-error") : ""}">${escapeHtml(row)}</li>`).join("");
    showToast(validation.valid ? "Test à blanc terminé sans erreur bloquante." : "Le test à blanc a détecté des erreurs.", validation.valid ? "success" : "error");
  }

  function executeValidatedFlow() {
    if (graph.status !== "validated") {
      showToast("Une version validée est requise avant exécution.", "error");
      return;
    }
    const validation = core.validateGraph(graph);
    if (!validation.valid) {
      showToast("Le flux validé est devenu incohérent. Créez une nouvelle version.", "error");
      return;
    }
    const now = new Intl.DateTimeFormat("fr-FR", { timeStyle: "medium" }).format(new Date());
    elements.runLog.innerHTML = `
      <li>[${now}] Demande d’exécution préparée pour la version ${graph.version}</li>
      <li>[${now}] Les étapes IA produiront des artefacts en Brouillon IA</li>
      <li class="log-success">[${now}] Prototype : aucune mutation serveur ni publication effectuée</li>
    `;
    showToast("Exécution simulée : la cible passera par l’API, le RBAC et l’audit.", "success");
  }

  function startDrag(event, nodeElement) {
    if (event.button !== 0 || event.target.closest("button")) return;
    const node = graph.nodes.find((item) => item.id === nodeElement.dataset.nodeId);
    if (!node) return;

    selectedNodeId = node.id;
    const initial = { x: event.clientX, y: event.clientY, nodeX: node.x, nodeY: node.y };
    let moved = false;
    nodeElement.setPointerCapture(event.pointerId);
    nodeElement.classList.add("dragging");

    function onMove(moveEvent) {
      moved = moved || Math.abs(moveEvent.clientX - initial.x) > 2 || Math.abs(moveEvent.clientY - initial.y) > 2;
      node.x = Math.max(20, Math.round(initial.nodeX + (moveEvent.clientX - initial.x) / zoom));
      node.y = Math.max(20, Math.round(initial.nodeY + (moveEvent.clientY - initial.y) / zoom));
      nodeElement.style.left = `${node.x}px`;
      nodeElement.style.top = `${node.y}px`;
      renderEdges();
    }

    function onEnd() {
      nodeElement.classList.remove("dragging");
      nodeElement.removeEventListener("pointermove", onMove);
      nodeElement.removeEventListener("pointerup", onEnd);
      nodeElement.removeEventListener("pointercancel", onEnd);
      if (moved) setDirty();
      render();
    }

    nodeElement.addEventListener("pointermove", onMove);
    nodeElement.addEventListener("pointerup", onEnd);
    nodeElement.addEventListener("pointercancel", onEnd);
  }

  elements.palette.addEventListener("click", (event) => {
    const button = event.target.closest("[data-add-kind]");
    if (button) addNode(button.dataset.addKind);
  });

  elements.nodes.addEventListener("pointerdown", (event) => {
    const nodeElement = event.target.closest(".flow-node");
    if (!nodeElement) return;
    startDrag(event, nodeElement);
  });

  elements.nodes.addEventListener("click", (event) => {
    const nodeElement = event.target.closest(".flow-node");
    if (!nodeElement) return;
    const nodeId = nodeElement.dataset.nodeId;
    const port = event.target.closest("[data-port]");
    selectedNodeId = nodeId;

    if (port && port.dataset.port === "output") {
      pendingSourceId = pendingSourceId === nodeId ? null : nodeId;
    } else if (port && port.dataset.port === "input") {
      connectNodes(nodeId);
      return;
    }
    render();
  });

  elements.edges.addEventListener("click", (event) => {
    const edgeElement = event.target.closest("[data-edge-id]");
    if (!edgeElement) return;
    graph.edges = graph.edges.filter((edge) => edge.id !== edgeElement.dataset.edgeId);
    setDirty();
    render();
    showToast("Connexion retirée du brouillon.", "info");
  });

  elements.nodeForm.addEventListener("input", () => {
    const selected = graph.nodes.find((node) => node.id === selectedNodeId);
    if (!selected) return;
    selected.label = elements.nodeLabel.value;
    selected.description = elements.nodeDescription.value;
    selected.ownerRole = elements.nodeOwner.value;
    selected.status = elements.nodeStatus.value;
    setDirty();
    renderNodes();
    renderEdges();
    renderHeader();
    renderValidation();
  });

  elements.graphName.addEventListener("change", () => {
    graph.name = elements.graphName.value.trim() || "Flux sans titre";
    setDirty();
    render();
  });

  elements.deleteNode.addEventListener("click", removeSelectedNode);
  elements.save.addEventListener("click", () => {
    if (saveGraph()) showToast(`Version ${graph.version} enregistrée localement.`, "success");
  });
  elements.submitReview.addEventListener("click", () => {
    if (saveGraph("review")) showToast("Version soumise à revue humaine.", "success");
  });
  elements.validate.addEventListener("click", () => {
    if (!window.confirm("Valider cette version du flux en tant qu’Architecte Applicatif ? Cette décision sera auditée dans la cible.")) return;
    if (saveGraph("validated")) showToast("Version validée. L’exécution est maintenant autorisée.", "success");
  });
  elements.test.addEventListener("click", runDryTest);
  elements.execute.addEventListener("click", executeValidatedFlow);
  elements.reset.addEventListener("click", () => {
    if (!window.confirm("Restaurer le flux de démonstration ? Le brouillon local courant sera remplacé.")) return;
    graph = core.createInitialGraph();
    snapshots = [];
    selectedNodeId = graph.nodes[0].id;
    pendingSourceId = null;
    dirty = false;
    persistState();
    render();
    showToast("Flux de démonstration restauré.", "info");
  });
  elements.fit.addEventListener("click", () => {
    zoom = 0.82;
    elements.viewport.scrollTo({ left: 0, top: 0, behavior: "smooth" });
    render();
  });
  elements.zoomIn.addEventListener("click", () => {
    zoom = Math.min(1.25, Number((zoom + 0.1).toFixed(2)));
    render();
  });
  elements.zoomOut.addEventListener("click", () => {
    zoom = Math.max(0.65, Number((zoom - 0.1).toFixed(2)));
    render();
  });

  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape") {
      pendingSourceId = null;
      render();
    }
    if ((event.key === "Delete" || event.key === "Backspace") && !event.target.matches("input, textarea, select")) {
      removeSelectedNode();
    }
    if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "s") {
      event.preventDefault();
      if (saveGraph()) showToast(`Version ${graph.version} enregistrée localement.`, "success");
    }
  });

  window.addEventListener("beforeunload", (event) => {
    if (!dirty) return;
    event.preventDefault();
    event.returnValue = "";
  });

  renderPalette();
  render();
})();
