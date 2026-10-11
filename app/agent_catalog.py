"""Catalogue fermé des clients, profils de compétence et capacités du relais."""

CLIENT_CATALOG = {
    "claude_code": "Claude Code",
    "codex": "Codex",
    "opencode": "OpenCode",
    "cursor": "Cursor",
    "windsurf": "Windsurf",
    "github_copilot": "GitHub Copilot",
    "kiro": "Kiro",
    "compatible": "Client compatible",
}

SKILL_CATALOG = {
    "backend": "Backend",
    "frontend": "Frontend",
    "qa": "QA",
    "devops": "DevOps",
    "security": "Sécurité",
    "tech-lead": "Tech Lead",
    "cto": "CTO",
}

CAPABILITY_CATALOG = {
    "task.runtime": "Démarrer le runtime client autorisé de sa réservation",
    "git.read": "Consulter les preuves Git de son équipe",
    "git.worktree": "Préparer un worktree pour sa réservation",
    "git.collect": "Collecter les preuves de sa tâche",
    "git.test": "Exécuter un test catalogué isolé",
    "git.review": "Rédiger un avis de revue consultatif",
    "session.heartbeat": "Maintenir la session",
    "session.disconnect": "Fermer la session",
    "task.claim": "Réserver une tâche prête",
    "task.release": "Libérer sa réservation",
    "message.send": "Transmettre un message lié à une tâche",
    "message.read": "Lire et recevoir la coordination de son profil",
    "message.ack": "Accuser réception du contexte transmis",
    "incident.report": "Signaler un incident technique",
    "incident.read": "Consulter les incidents et notifications de son équipe",
    "incident.ack": "Acquitter une notification de son profil",
    "memory.read": "Lire les références mémoire du projet",
    "memory.propose": "Proposer un brouillon mémoire au projet",
}

# Toutes les déclarations restent bornées par les permissions API et le profil du projet.
CLIENT_CAPABILITIES = {code: frozenset(CAPABILITY_CATALOG) for code in CLIENT_CATALOG}
SKILL_CAPABILITIES = {code: frozenset(CAPABILITY_CATALOG) for code in SKILL_CATALOG}
