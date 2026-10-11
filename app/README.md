# Templates IHM Floppy-MimIC

Ce dossier amorce les IHM serveur de la V1 avec Jinja2. Il reprend les parcours décrits dans [l'IHM globale](../docs/ihm/001-global-ihm.md) et le style du [prototype](../docs/ihm/prototype/index.html). L'API réelle couvre identité/ACL, configuration LLM, sources, contexte, génération et régénération documentaires, revue humaine, versions/diff/restauration, publication/export privés, coordination gouvernée des sessions, sprints et mémoire versionnée ; les autres services métier restent à implémenter.

## Structure

- `templates/base.html` : navigation, messages et ressources communes.
- `templates/partials/ui.html` : macros pour statuts, états vides et jetons CSRF.
- `templates/pages/` : projets, sources, contexte, flux, pilotage, équipe IA, générations, revue, versions, publications, audit, membres et erreurs.
- `static/css/prototype.css` : copie du style de référence du prototype, conservé dans `docs/ihm/prototype/`.
- `static/css/app.css` : compléments pour les pages alimentées par le serveur.
- `preview.py` : routes GET de présentation, sans base de données, API métier ni mutation.
- `requirements-preview.txt` : dépendances minimales pour parcourir les templates.
- `main.py`, `identity_service.py`, `identity_models.py` : routes et services réels du lot identité, projet et ACL.
- `migrations/` : migrations Alembic identité, LLM, sources, contexte, livrables, revue/publication/versionnement, agents, sprints, mémoire, conversations et résultats CoFFY, jusqu'à `0026_coffy_results` ; `identity_cli.py` : création interactive du premier administrateur.
- `agent_api.py`, `agent_service.py`, `agent_models.py` et `0008_agent_sessions` : plan de contrôle JSON des sessions techniques d'agents.
- `agent_catalog.py`, `agent_routing.py`, `0009_agent_teams_catalog` et `0010_agent_task_reservations` : équipes projet, profils de compétence versionnés, capacités fermées et réservations préparatoires.
- `memory_models.py`, `memory_service.py`, `memory_api.py` : connaissances persistantes, décisions versionnées, références actives et partage global exact.
- `pilotage_service.py`, `pilotage_api.py` : projection projet en lecture, HTML et JSON, compteurs de l’équipe IA et liste filtrable sous ACL.
- `requirements-identity.txt` : dépendances minimales de ce lot, extraites du socle Python.
- `llm_models.py`, `llm_service.py`, `requirements-llm.txt` : profils cloud/locaux versionnés, client LiteLLM et dépendances associées.
- `object_storage.py`, `worker.py`, `requirements-infra.txt` : accès S3 privé et jobs Celery/Redis. `requirements-test.txt` ajoute pytest et HTTPX.
- `business_models.py`, `ingestion_service.py`, `context_service.py`, `generation_service.py`, `review_service.py`, `versioning_service.py`, `requirements-ingestion.txt` : sources, extraction contrôlée, génération/régénération, versions et diff, décisions humaines et référence publiée.

## Lancer le lot identité et ACL

Prérequis : PostgreSQL accessible, base dédiée déjà créée, Python 3.12+, connexion HTTPS en environnement partagé. Depuis la racine du dépôt :

```sh
python -m venv .venv
source .venv/bin/activate
python -m pip install -r app/requirements-infra.txt
export MIMIC_DATABASE_URL='postgresql+asyncpg://UTILISATEUR:MOT_DE_PASSE@HOTE:5432/mimic'
export MIMIC_AUTH_KEY="$(python -c 'import secrets; print(secrets.token_urlsafe(48))')"
python -m alembic upgrade head
python -m app.identity_cli bootstrap-admin --email admin@example.org --name 'Administrateur'
python -m uvicorn app.main:app
```

`MIMIC_AUTH_KEY` doit être conservée dans une configuration secrète stable entre les processus ; sa rotation invalide la pseudonymisation des compteurs de connexion. Pour un accès HTTP **uniquement local** à `127.0.0.1`, définir `MIMIC_COOKIE_SECURE=false` avant le démarrage ; laisser `true` en HTTPS. Ne jamais committer les valeurs réelles. Le bootstrap refuse de s'exécuter si des comptes existent déjà et demande le mot de passe sans l'afficher.

L'administrateur se connecte sur `/login`, crée une invitation dans `/admin/users`, transmet manuellement l'adresse et le code affiché une fois, puis ajoute le compte au projet dans l'écran `Membres`. L'invité active son compte sur `/activate` et se connecte. Les codes expirent après 48 heures. La liste `/projects`, les fiches projet, Sources et Contexte sont filtrés côté API ; les autres écrans métier demeurent dans l'aperçu séparé ci-dessous.

## Configurer des LLM cloud ou locaux

Après `alembic upgrade head`, l'administrateur ouvre `/admin/llm`. Les profils V1 acceptent OpenAI, Azure OpenAI, Anthropic, Ollama et une API compatible OpenAI (vLLM ou service privé). Les identifiants de modèle sont saisis sans préfixe. Pour Azure, saisir le nom du déploiement et la version d'API ; pour Ollama, utiliser la base de son service (habituellement `http://127.0.0.1:11434`) ; pour une API compatible OpenAI, utiliser sa base `/v1`.

L'exploitant configure les variables de clé dans l'environnement **de l'API et des workers**. Le formulaire reçoit seulement leur nom, au format `MIMIC_LLM_KEY_*`. Les bases d'API Azure ou locales doivent être déclarées à l'identique dans `MIMIC_LLM_ALLOWED_BASE_URLS`, liste séparée par des virgules ; par exemple `http://127.0.0.1:11434,https://ressource.openai.azure.com`. OpenAI et Anthropic utilisent leurs destinations officielles sans adresse saisie. Les valeurs de clés ne sont jamais enregistrées en base ni affichées.

Le bouton « Vérifier » déclenche **sur demande** un appel minimal au fournisseur et peut consommer des tokens. La version courante doit réussir cette vérification avant de devenir le modèle par défaut. Modifier ou désactiver le profil par défaut retire ce choix ; la nouvelle version doit être vérifiée puis sélectionnée explicitement. Une vérification échouée retire également le défaut concerné. Les appels applicatifs passent par `app.llm_service.invoke_profile` ; l'analyse documentaire l'utilise maintenant, tandis que les générations de livrables et tâches agentiques restent dans la [ToDO](../TODO.md). Une politique de sortie réseau doit limiter les processus API et workers aux destinations approuvées.

## Socle local PostgreSQL, Redis et stockage S3

Pour développer et vérifier le socle sans services externes, `compose.dev.yml` lance PostgreSQL 16, Redis et **MinIO** sur des ports liés à `127.0.0.1`. Les mots de passe et clés temporaires sont générés hors du dépôt ; les mêmes clés configurent MinIO et le client S3 de l'application :

```sh
python scripts/dev_env.py /private/tmp/mimic-dev.env
set -a
source /private/tmp/mimic-dev.env
set +a
docker compose --env-file /private/tmp/mimic-dev.env -f compose.dev.yml up -d --wait
python -m pip install -r app/requirements-test.txt
python -m alembic upgrade head
python -m scripts.init_dev_bucket
python -m pytest -q tests
```

Le fichier d'environnement est créé en mode `0600` et ne doit pas être ajouté au dépôt. L'API utilise `mimic_dev` et la suite de tests utilise exclusivement `mimic_test` ; les tests effacent leur base dédiée avant chaque cas d'intégration. La console MinIO locale est accessible sur `http://127.0.0.1:55001`, avec les identifiants du fichier d'environnement. Une seconde console ayant chargé ce fichier doit lancer le worker : `python -m celery -A app.worker:celery_app worker --loglevel=INFO`. Pour utiliser l'IHM réelle, créer son premier administrateur avec la commande indiquée plus haut puis lancer `python -m uvicorn app.main:app`. La tâche `mimic.storage.verify` reçoit seulement une clé opaque et une empreinte SHA-256 ; les tâches `mimic.source.extract` et `mimic.source.analyze` ne reçoivent que des identifiants. Aucun document ne transite dans Redis. Le client S3 limite chaque objet à 25 Mio, évite les noms de fichiers dans les clés et n'émet aucune URL publique.

Conserver le **même fichier d'environnement** lors des redémarrages : `dev_env.py` crée de nouveaux secrets, mais ne change pas les identifiants d'une base PostgreSQL ou d'un MinIO déjà initialisés. Si le port `8000` est occupé, lancer l'API avec `--host 127.0.0.1 --port 8001`. Les identifiants du premier administrateur se définissent par la commande `bootstrap-admin` ; aucune valeur par défaut n'est intégrée au dépôt.

## Parcours Sources et Contexte

## Tâches récurrentes CoFFY-02 à 05

Après `python -m alembic upgrade head`, l'écran `/projects/{id}/scheduled-tasks` permet au Chef de Projet de créer des brouillons de tâche LLM, de les réviser par version, de gérer les sujets du projet et de consulter leurs historiques. Les membres du projet avec `scheduled_task.read` peuvent consulter sans modifier. L'API JSON utilise `/api/projects/{id}/scheduled-tasks` et `/api/projects/{id}/scheduled-subjects` ; chaque mutation exige un cookie de session et `X-CSRF-Token`, ainsi qu'une révision attendue pour modifier ou changer un statut. Le profil LLM est désigné par l'ID exact de sa version ; aucun secret ou URL fournisseur n'est enregistré dans la tâche. L'activation exige un profil vérifié et calcule la prochaine échéance. La tâche active peut être exécutée immédiatement avec une clé UUID d'idempotence. L'API expose `/{task_id}/runs`, `/{task_id}/runs/{run_id}`, `/{task_id}/runs/{run_id}/results` et `/{task_id}/occurrences` sous ACL projet. Le détail HTML d'un run affiche ses tentatives, erreurs, durées, résultats, commentaires et avis.

Un run réussi crée une première version `Brouillon IA`. Un membre non lecteur peut soumettre ou corriger ; le Product Owner valide le domaine métier, l'Architecte Applicatif le domaine technique et le Chef de Projet le pilotage. Chaque transition produit une nouvelle version auditée. Les avis 1..5 et commentaires n'agissent jamais sur ce statut. Le mode JSON strict accepte un document JSON complet sans clés dupliquées, constantes hors JSON, valeur non finie ou Unicode invalide ; aucun schéma métier supplémentaire n'est supposé. Aucune sortie n'est publiée ni introduite automatiquement dans Contexte ou Livrables : leurs workflows humains demeurent nécessaires pour une référence projet.

La page `/projects/{id}/pilotage` et son API JSON ajoutent une section CoFFY sous `pilotage.read` et `scheduled_task.read`. Elle filtre les runs par 7/30/90 jours glissants UTC ou tout l'historique, statut, sujet, fournisseur et version de profil/modèle. Elle montre volumes, occurrences sautées et échéances coalescées, taux sur runs terminaux, durées mesurées, erreurs fréquentes et ventilations fournisseur/modèle. La satisfaction retient le dernier avis par auteur et run ; les comptes et dénominateurs sont indiqués, sans texte d'avis ni décision implicite. Les coûts, budgets et quotas restent dans les sections existantes. COFFY-05 n'ajoute aucune migration.

L'ordonnanceur utilise le broker Redis déjà configuré : lancer **un seul** processus Beat avec `python -m celery -A app.worker:celery_app beat --loglevel=INFO` et au moins un worker avec `python -m celery -A app.worker:celery_app worker --loglevel=INFO`. Le tick `mimic.schedule.tick` s'exécute chaque minute et transmet seulement les identifiants des runs ; le worker `mimic.coffy.execute` applique de nouveau droits, profil, quotas et budgets. `once` attend ISO 8601 avec décalage ; `interval` utilise un nombre de secondes entre 60 et 2 592 000 ; `cron` utilise cinq champs et un fuseau IANA. Une panne du broker laisse les runs en attente pour un dispatch ultérieur.

Le retour arrière de `0026_coffy_results` vers `0025_coffy_execution` supprime versions, commentaires et avis CoFFY. Celui de `0025` vers `0024` supprime occurrences, runs, tentatives et compteurs ; celui de `0024` vers `0023_organization_scope` supprime aussi les définitions. Ces commandes sont réservées aux bases jetables sans conservation préalable des preuves métier. Les tables sont temporairement dans `public` avant le lot DB-01 à DB-10.

Depuis `/projects/{id}/sources`, un membre habilité importe un PDF, DOCX, Markdown, HTML ou XLSX, ou saisit un besoin. L'import vérifie extension, signature/contenu et taille, conserve le fichier en S3 privé, puis lance l'extraction Celery. La fiche source affiche l'état et, une fois l'extraction terminée, propose « Lancer l'analyse IA ». Cette action exige un profil LLM par défaut vérifié et disponible ; chaque tentative garde son identifiant de version de profil, ses compteurs de tokens éventuels et une catégorie d'erreur sans prompt ni secret. Une relance conserve les propositions précédentes.

Depuis `/projects/{id}/generations`, un membre avec `generation.create` choisit un type documentaire, un style contrôlé et de 1 à 20 éléments de contexte non rejetés et non obsolètes du même projet. Un profil LLM par défaut disponible est requis. L'API fige les versions du contexte et du profil, puis envoie au worker `mimic.document.generate` le seul identifiant du run. L'écran montre les états techniques et permet une relance explicite après échec. Le succès crée uniquement une version `Brouillon IA` ; la fiche liée affiche sa provenance et peut produire une nouvelle version humaine `À revoir`.

Depuis `/projects/{id}/reviews`, le membre soumet le brouillon, puis le Product Owner ou l'Architecte Applicatif décide selon le type du livrable. Le rejet exige un motif ; les commentaires restent attachés à la version lue et peuvent être résolus. Depuis `/projects/{id}/publications`, le Chef de Projet publie une version `Validé`, puis demande un export Markdown ou JSON. `mimic.export.artifact` reçoit uniquement l'ID de l'export, produit le fichier dans S3 privé et conserve son empreinte ; le téléchargement passe par l'API avec ACL et contrôle d'intégrité. Une nouvelle publication rend l'ancienne référence obsolète. Les états d'échec de dispatch ou stockage sont visibles et une nouvelle demande d'export est possible.

Depuis `/projects/{id}/versions`, tout membre avec `version.compare` choisit deux versions du même livrable. La tâche `mimic.version.diff` reçoit seulement l'ID du diff ; l'écran présente le résultat ou l'erreur et compare la provenance des deux versions. Un membre avec `generation.create` peut confirmer la restauration d'une version historique : une nouvelle version `À revoir` est créée. Dans le détail d'une version courante, il peut fournir un motif, un contexte actuel et une consigne additionnelle pour la régénération. La tâche `mimic.document.generate` conserve le parent et vérifie qu'il reste courant ; elle produit uniquement un nouveau `Brouillon IA`. Une tentative échouée peut être relancée explicitement si le parent reste courant. Aucune sortie IA n'est validée automatiquement.

Dans `/projects/{id}/context`, le Product Owner ou l'Architecte peut corriger et versionner le contenu. Toute proposition IA commence en `Brouillon IA`, passe à `À revoir` sur décision humaine, puis est `Validé` ou `Rejeté` par le rôle habilité selon son type. Les éléments manuels commencent en `À revoir`. Le lecteur peut consulter sans modifier. Les relations entre éléments, commentaires et retraits logiques sont également actifs ; les autres écrans métier suivent la [ToDO](../TODO.md).

L'image `alpine/minio` du compose est une reconstruction communautaire épinglée de MinIO Community et sert uniquement au développement local. Pour un déploiement durable, sélectionner une distribution MinIO maintenue et gérer sa licence éventuelle selon la [documentation officielle](https://docs.min.io/aistor/installation/container/install/). Configurer `MIMIC_S3_ENDPOINT_URL`, `MIMIC_S3_BUCKET`, `MIMIC_S3_REGION`, `MIMIC_S3_ACCESS_KEY`, `MIMIC_S3_SECRET_KEY` et `MIMIC_REDIS_URL` dans le gestionnaire de secrets ; HTTPS est exigé pour S3 sauf `MIMIC_S3_ALLOW_HTTP=true` sur l'hôte local. Le bucket doit être privé et créé par l'exploitant.

## API des équipes et sessions d'agents (AGENT-01 à 05)

Après `alembic upgrade head`, un Architecte Applicatif crée une équipe du projet via `POST /api/projects/{project_id}/agent-teams` avec cookie, `X-CSRF-Token` et `{"name":"Équipe API","skill_codes":["backend","qa"]}`. `GET /api/projects/{project_id}/agent-catalog` liste les clients, compétences et capacités autorisés ; `GET .../agent-teams` liste les équipes du projet. Un membre non lecteur peut ensuite appeler `POST /api/projects/{project_id}/agent-sessions` avec son cookie, le CSRF et `{"team_id":"<uuid>","client_code":"codex","skill_code":"backend","capabilities":["session.heartbeat","session.disconnect"]}`. La réponse `201` affiche le jeton opaque **une seule fois** ; le client doit le conserver hors de la base métier et l'envoyer dans `Authorization: Bearer <jeton>` à `POST .../{session_id}/heartbeat` toutes les 120 secondes au maximum. `POST .../{session_id}/disconnect` coupe la session si cette capacité a été déclarée. Sa durée absolue est de 12 heures.

`GET /api/projects/{project_id}/agent-sessions` exige `agent.read` et ne retourne aucun secret. `DELETE .../{session_id}` exige le rôle Chef de Projet (`agent.run.manage`) et un CSRF. Les sessions expirées et révoquées ne peuvent plus émettre de heartbeat. AGENT-04 permet à l'Architecte de créer `POST .../agent-flows`, puis de revoir et valider la version ; le Chef de Projet lance `POST .../{version_id}/runs`, propose un plan via `POST .../runs/{run_id}/plans`, le soumet en revue puis l'approuve. Chaque mutation demande cookie humain et CSRF. `GET .../agent-tasks/ready?session_id=...` liste les tâches compatibles après approbation ; `POST .../agent-tasks/{task_id}/claim?session_id=...` réserve pour 120 secondes au plus, idempotemment. Ces deux routes vérifient jeton, capacité et permission métier. `POST .../agent-messages` exige désormais une tentative propre et le plan approuvé ; voir AGENT-12 plus bas. Le [cas d'usage](../docs/use-cases/016-agentic-workflows.md) et la [preuve](../docs/quality/proofs/agent-04.md) détaillent le contrat. Cette API est le plan de contrôle du relais ; elle ne parle pas encore le protocole MCP.

## Voir les écrans

Depuis la racine du dépôt, installer les dépendances minimales de l'aperçu, puis lancer :

```sh
python -m pip install -r app/requirements-preview.txt
python -m uvicorn app.preview:app --reload
```

Ouvrir `http://127.0.0.1:8000/projects`. Le bandeau bleu signale les données de présentation. Les formulaires POST sont désactivés dans cet aperçu ; il n'exécute aucune analyse IA, décision humaine ni publication. Les routes de détail utilisent l'identifiant fictif `preview`.

## Contrat de rendu FastAPI

Monter les fichiers statiques sous le nom `static` et rendre les pages avec `Jinja2Templates(directory="app/templates")`. Chaque réponse fournit `request`, `current_user`, `permissions`, `csrf_token`, `filters` et `metrics`. Les vues de projet fournissent `project` avec `id` et `name` ; les autres objets sont indiqués par les noms utilisés dans chaque template. Le moteur doit garder l'auto-échappement HTML actif.

Les chemins `/projects/...` dans les templates forment le contrat des routes métier. Les routes identité, création/lecture projet, membres, Sources, Contexte, Générations, Revue, Versions et Publications sont actives dans `app.main` ; l'aperçu `app.preview` reste indépendant. Les URL fournies par les données (`href`, `output_href`) doivent être des chemins internes autorisés. Les actions `POST` exigent une vérification CSRF et une autorisation RBAC côté API, sans déduire le droit d'un bouton visible. Les listes et détails doivent être filtrés par projet avant rendu.

Les corrections de contexte et de livrable créent une nouvelle version. Les formulaires de validation, rejet, approbation de plan, publication et export doivent appliquer les transitions et audits documentés dans les [specs](../docs/specs/specs.md), le [modèle de données](../docs/data/001-core-data-model.md) et le [modèle de sécurité](../docs/security/001-core-security-model.md). Aucun template ne déclenche lui-même un appel LLM ou un traitement asynchrone.

## Suite d'implémentation

1. Étendre les guards identité/ACL aux routes de lecture des autres écrans métier au moment de leur raccordement.
2. Raccorder les autres formulaires aux services métier avec RBAC, CSRF, validation serveur et audit.
3. Compléter le canevas interactif des flux et la vue graphe des plans agentiques sans déplacer les règles métier dans le navigateur.
4. Ajouter les états de chargement, les retours d'erreur contextualisés et les adaptations mobiles lors de l'intégration réelle.

### Tâches hiérarchiques et progression (AGENT-06/07)

Appliquer `python -m alembic upgrade head` pour `0012_agent_task_hierarchy` avant lancement. La proposition de plan reçoit pour chaque tâche `parent_task_key` facultatif, `priority` (P0–P3, défaut P2) et `due_at` ISO 8601 avec fuseau. Le parent est une clé de la même proposition, jamais un ID d'un autre plan. Trois niveaux au total sont autorisés ; parents peuvent être déclarés après leurs enfants.

`GET /api/projects/{project_id}/agent-tasks?plan_id=...` rend la liste JSON aux membres avec `agent.read` : parent, profondeur, profil versionné, priorité, échéance, état technique et pourcentage. `agent-tasks/ready` expose seulement les feuilles compatibles ; un claim direct sur un parent est refusé.

Après claim, `POST .../agent-tasks/{task_id}/progress?session_id=...` avec Bearer, capacité `task.claim` et permission `agent.task.claim` du créateur reçoit `{"claim_id":"ID de la réservation courante","progress_percent":50}`. Entier 0–100, sans régression. À 100 la feuille est `completed` et sa réservation clôturée ; le rejeu exact n'a pas de nouvel effet. Les parents reçoivent la moyenne entière de leurs enfants directs dans la même transaction et ne sont terminés que si tous les enfants le sont. Une libération conserve le pourcentage, mais l'ancien claim ne peut plus le modifier. Aucun livrable ni run n'est validé ou terminé automatiquement.

Le downgrade de `0012` détruit les nouveaux champs : base jetable ou restauration coordonnée uniquement. Les écrans Équipe IA restent prototypes ; ce lot livre le contrat API, pas une nouvelle interface. Voir [cas et critères](../docs/use-cases/016-agentic-workflows.md) et [preuves](../docs/quality/proofs/agent-06-07.md).

### Coordinateur des tâches (AGENT-08)

Appliquer `python -m alembic upgrade head` pour `0013_agent_task_coordinator`. Proposition du plan : `dependency_keys` par tâche, `max_parallel_tasks` (1–8, défaut 4) et `max_attempts` (1–5, défaut 3). Parents : dépendances héritées et cibles développées en feuilles ; tout cycle induit est refusé. Limites et dépendances restent figées avant approbation humaine.

- `POST .../agent-tasks/claim-next?session_id=...`, Bearer, JSON `{"request_key":"UUID"}` : prochain candidat compatible, tri priorité/échéance/clé ; retour claim et tentative. Une même clé de session renvoie l'historique, y compris après clôture, sans réattribution.
- `POST .../agent-tasks/{id}/fail?session_id=...`, Bearer, JSON `{"claim_id":"UUID","error_code":"execution_error"}` : clôture de sa tentative. Autres catégories : `timeout`, `invalid_result`.
- `POST .../agent-tasks/{id}/retry`, cookie humain et CSRF, JSON `{"attempt_id":"UUID"}` : relance d'une feuille échouée, tentative attendue et budget contrôlés.
- `POST .../agent-tasks/{id}/cancel` : annule le sous-arbre non terminé, sous `agent.run.manage` et CSRF.
- `POST .../agent-flows/plans/{id}/pause`, `/resume`, `/cancel` : contrôles humains du run approuvé. Une pause clôt les claims, une reprise ne les réactive pas ; un run annulé reste terminal.
- `GET .../agent-tasks/{id}/attempts` : historique sous `agent.read`. La liste du plan rend aussi limites, dépendances effectives et blocages.

Les claims directs existants appliquent les mêmes gardes. Toute réservation consomme une tentative ; expiration impose 2, 4, 8… secondes de délai (60 maximum), puis reprise dans le budget. Échec explicite exige retry humain ; libération volontaire/pause sans nouveau délai. Progression conservée, ancienne tentative immuable. À épuisement : `failed`, aucune extension de limite en place. Aucun shell ou LLM n'est lancé ; le coordinateur sert les clients HTTP existants. Les écrans et le transport MCP restent ouverts. [Cas](../docs/use-cases/016-agentic-workflows.md), [preuve](../docs/quality/proofs/agent-08.md).

### Autonomie et approbations (AGENT-09)

Appliquer `python -m alembic upgrade head` pour `0014_agent_action_approval`. La proposition de plan reçoit `autonomy_mode` : `bounded` (défaut), `sensitive` ou `per_action`. Chaque tâche reçoit `action_class` : `standard` (défaut) ou `sensitive`. Sensibilité héritée des parents. Mode borné : sensible refusé ; mode sensible : accord requis par tentative sensible ; action par action : accord pour toute tentative de feuille. Mode/classes figés ; changer le mode nécessite un nouveau plan/run approuvé.

- `POST .../agent-tasks/{id}/approval-request`, cookie/CSRF et `agent.run.manage`, JSON `{"attempt_number":1}` : ouvre ou retrouve la demande pour la prochaine tentative attendue. Une demande peut être préparée avant satisfaction des dépendances/délais, qui restent contrôlés au claim.
- `GET .../agent-tasks/{id}/approvals`, `agent.read` : historique de demandes, décisions, consommation et invalidation.
- `POST .../agent-tasks/{id}/approvals/{approval_id}/approve` ou `/reject`, cookie/CSRF et `agent.plan.approve` : décide la demande exacte. Refus annule la feuille ; rejeu identique rapporte l'historique sans nouvel effet, choix opposé `409`.

La consultation technique prête et les claims ouvrent automatiquement les demandes nécessaires sous Bearer, sans pouvoir décider. Le claim direct et `claim-next` appliquent la même porte ; une demande en attente ne consomme aucun créneau ou tentative. Toutes les autres gardes restent actives. Accord consommé atomiquement, relié à `agent_task_run.approval_id` et au claim unique. Nouveau numéro après libération/expiration/échec = nouvel accord. Pause/annulation invalide les accords inutilisés ; reprise ne les restaure pas, nouvelle demande avec nouvel ID même si le numéro reste inchangé.

La liste humaine du plan rend aussi mode, classe effective, porte requise et demande active, avec `approval_required`/`approval_pending` dans `blocked_by`. Aucun nouveau droit d'outil, état documentaire ou écran réel ; les budgets/seuils, runtime et transport MCP restent leurs lots. Les plans antérieurs restent bornés, leurs tâches standard et leurs tentatives sans approbation rétroactive. Downgrade destructif réservé à base jetable/restauration coordonnée. [Cas](../docs/use-cases/016-agentic-workflows.md), [ADR](../docs/adr/013-agent-action-approval.md), [preuve](../docs/quality/proofs/agent-09.md).

### Sprints et kanban (AGENT-10)

Appliquer `python -m alembic upgrade head` pour `0015_agent_sprints` avant lancement. Aucun nouveau service ni variable d'environnement. La navigation **Sprints** ouvre `/projects/{project_id}/agent-sprints` : liste, choix d'un plan approuvé et formulaire de brouillon. Le détail `/{sprint_id}` rend board, calendrier, indicateurs, filtres et historique, ainsi que la révision du brouillon. Dates saisies/affichées en heure de Paris, heures de changement d'heure ambiguës/inexistantes refusées. Les formulaires et l'API partagent services et guards.

Préfixe JSON : `/api/projects/{project_id}/agent-sprints`. Lecture sous cookie humain et `agent.read` ; mutations sous `agent.run.manage` et en-tête `X-CSRF-Token`.

| Route | Contrat |
| --- | --- |
| `POST` préfixe | Créer un brouillon sur plan approuvé/run actif ou suspendu ; retour `201`. |
| `GET` préfixe | Lister les calendriers et statuts du projet. |
| `GET /{sprint_id}` | Board sans mutation : `sprint`, `as_of`, `period_ended`, `counts`, `metrics`, `cards`. |
| `PUT /{sprint_id}` | Réviser uniquement le brouillon à `expected_revision` ; JSON complet sans `plan_id`. |
| `GET /{sprint_id}/history` | Snapshots successifs avec action, acteur, révision et date. |
| `POST /{sprint_id}/start`, `/close`, `/cancel` | JSON `{"expected_revision":1}` ; aucune transition de tâche/run/accord. |

Exemple de création (remplacer les UUID et choisir une période future ou en cours) :

```json
{
  "plan_id": "UUID-du-plan-approuve",
  "name": "Sprint de préparation",
  "goal": "Suivre les feuilles et les décisions attendues",
  "starts_at": "2026-10-05T09:00:00+02:00",
  "ends_at": "2026-10-19T18:00:00+02:00",
  "items": [{"task_id": "UUID-feuille", "target_due_at": "2026-10-15T18:00:00+02:00"}]
}
```

Période positive ≤ 90 jours, fin future à création/révision, 0–20 feuilles distinctes du même plan ; démarrage uniquement pendant la période avec sélection non vide. Une nouvelle sélection terminale, un parent ou un chevauchement pour la même feuille est refusé. Calendrier/sélection figés sur sprint actif ; clôture anticipée permet un autre sprint à partir de `closed_at`. Révision périmée ou transition impossible : `409` ; schéma invalide : `422` ; référence étrangère : `404`. Rejeu immédiat d'une transition identique avec la révision d'origine ou courante sans nouvel effet.

Filtres GET combinables : `column=todo|in_progress|blocked|done|cancelled`, `overdue_only=true`, `blocked_only=true`. Les cartes sont filtrées ; compteurs et métriques gardent toute la sélection. Échéance effective = date cible, sinon date du plan, sinon fin du sprint ; sans modification du dispatch. Les blocages reprennent dépendances, états, limites, délais, autonomie et sessions/baux expirés. Lire n'expire aucun claim ni ne crée un accord. Les incidents manuels restent leur lot.

Throughput : feuilles achevées dans la période après rattachement ; cycle time moyen : premier démarrage → achèvement de ces mêmes feuilles, retries/attentes inclus. Parents et tentatives intermédiaires ne doublent pas les comptes. Pas d'échantillon = `null`, pas zéro ; durée historique des blocages = `null`. Sprint clos : cartes actuelles, fenêtre de mesure arrêtée à la clôture. Projet archivé consultable mais non modifiable. Les commandes d'approbation/relance d'une tâche restent les API AGENT-08/09 ; cet écran suit les causes et l'acteur attendu.

Tests : `python -m pytest tests/test_agent_sprints.py -q --tb=short` sur l'environnement d'intégration existant. La migration et son retour à `0014` sont vérifiés sur base jetable : les tables de sprint disparaissent au downgrade, claims/tentatives restent conservés. [Cas et critères](../docs/use-cases/016-agentic-workflows.md), [ADR 014](../docs/adr/014-agent-sprint-projection.md), [preuve](../docs/quality/proofs/agent-10.md).

### Mémoire entre conversations et sessions (AGENT-11)

Appliquer `python -m alembic upgrade head` pour `0016_project_memory`. Les connaissances sont persistées dans PostgreSQL, indépendamment des sessions. Un client de nouvelle conversation récupère les références via l'API ; aucun transcript de chat n'est collecté et aucun contexte n'est injecté automatiquement dans un runtime. Les écrans Mémoire sont livrés dans AGENT-14.

Préfixe humain : `/api/projects/{project_id}/memories`. Cookie actif et `memory.read` pour lire ; `X-CSRF-Token` et `memory.propose` pour proposer/corriger/soumettre (PO/AA/CP). Décision selon le type : AA pour `convention`/`architecture_decision`, PO pour `business_constraint`, CP pour `result`. Les lecteurs n'écrivent pas. Un projet archivé reste consultable humainement, sans mutation.

| Route | Contrat |
| --- | --- |
| `POST` préfixe | Créer une mémoire projet `À revoir`, type immuable et première version. |
| `GET` préfixe | Liste paginée (`limit` 1–100, défaut 50, `offset`) et filtre `kind`. |
| `GET /{memory_id}`, `GET /{memory_id}/versions` | Dernière version, ID de référence active et historique immuable. |
| `POST /{memory_id}/versions` | Contenu complet et `expected_version`, nouvelle version `À revoir`. |
| `POST /{memory_id}/submit`, `/approve`, `/reject`, `/retire` | JSON `{"expected_version":1,"reason":""}` ; motif non vide pour rejet/retrait. |
| `GET /context` | Références actives uniquement ; `q` littéral ≤ 200, `kind`, `limit` 1–20, `max_chars` 500–12000. |
| `GET /global-references` | Versions globales explicitement partagées au projet, avec indicateur `usable`. |
| `POST /global-references/{grant_id}/revoke` | CP (`agent.run.manage`) et CSRF ; retrait du partage sans effacement d'historique. |

Exemple de création :

```json
{
  "kind": "result",
  "title": "Tests de référence",
  "content": "Les tests métier utilisent une base isolée et des références explicites.",
  "source_note": "Revue de l'équipe du projet",
  "source_refs": [],
  "confidence": null,
  "change_note": "Proposition initiale"
}
```

Titre 180, contenu 4000, source déclarée 500, motif 1000 caractères maximum ; confiance facultative 0–1. Sources facultatives jusqu'à dix références distinctes `{"type":"source_document|context_item_version|deliverable_version|agent_task_run","id":"UUID"}`, toujours du même projet. Les signatures reconnaissables de secrets sont refusées ; utiliser des noms de configuration, jamais les valeurs de credentials. Réponses non mises en cache, erreurs de schéma sans écho du contenu.

Une proposition/correction ne modifie jamais une ancienne version ni la référence active existante. Valider crée une version `Validé` et la rend référence ; rejeter laisse la référence précédente active. Retirer crée `Obsolète` et vide le pointeur. Réouvrir nécessite une nouvelle revue/décision. `expected_version` protège des conflits (`409`) ; rejeu immédiat de décision identique avec même motif et version d'origine/courante sans nouvel audit/version. Décision opposée, schéma invalide ou mauvais rôle refusés.

Préfixe technique : `/api/projects/{project_id}/agent-memory`. Enregistrer une session avec `session.heartbeat`, `memory.read` et/ou `memory.propose` ; les sessions antérieures gardent leurs capacités initiales et doivent être réenregistrées si nécessaire. Bearer, `session_id` UUID et permission correspondante du créateur sont exigés à chaque appel.

- `POST` préfixe : même contenu/type, crée **Brouillon IA** avec session de provenance.
- `POST /{memory_id}/versions` : contenu et version attendue, nouvelle proposition IA, sans remplacer l'accord existant.
- `GET /context` : mêmes filtres/budget que l'humain, seulement références actives validées et partages utilisables. Une seconde session du même projet retrouve les mêmes versions.

Une soumission humaine crée `À revoir` avant décision. Le Bearer n'a aucune route de validation/partage globale ni accès à l'historique humain. Le contexte renvoie `usage=reference_data_only`, IDs/numéros/parent, source et références, confiance, validateur/date et signal de troncature. Budget de texte = titre + contenu + source déclarée ; métadonnées distinctes, motifs libres exclus. Élément trop long omis entier. Aucun texte mémoire ne constitue une instruction système, droit d'outil ou approbation ; le client/runtime doit préserver cette séparation.

Préfixe Administrateur : `/api/global-memories`, avec même création/liste/détail/versions/décisions, cookie/CSRF en mutation. **Conventions seulement**, sans référence privée projet, pas de promotion automatique depuis une mémoire projet. `POST /{memory_id}/grants` avec `{"project_id":"UUID","expected_version":2}` partage la version courante validée exacte. Une correction en revue laisse l'ancien partage actif ; nouvelle validation/remplacement ou retrait le rend inutilisable dans le contexte. Nouveau partage explicite nécessaire, sans propagation silencieuse. Le catalogue global et ses brouillons restent réservés à l'Administrateur.

Migration/retour arrière testés sur base jetable : downgrade vers `0015_agent_sprints` détruit mémoires/versions/partages et permissions ajoutées, mais conserve sprints, claims et tentatives. Tests : `python -m pytest tests/test_project_memory.py -q --tb=short`. [Cas](../docs/use-cases/016-agentic-workflows.md), [ADR 015](../docs/adr/015-versioned-project-memory.md), [preuve](../docs/quality/proofs/agent-11.md).

### Démarrages depuis les dates de sprint — exigence AGENT-31

La règle retenue prévoit **une date de démarrage par tâche**, comprise dans le sprint, distincte de son échéance. Un futur ordonnanceur déclenchera l'attribution/exécution à cette date si les dépendances, créneaux, limites, droits et accords humains sont satisfaits ; sinon l'attente reste visible. AGENT-11 ne livre pas encore cet ordonnanceur ni ce champ. Raccordement au client connecté/runtime AGENT-21, reprise après indisponibilité, idempotence et erreurs devront être vérifiés dans [AGENT-31](../TODO.md) et son [cas UC-016-03G](../docs/use-cases/016-agentic-workflows.md).

### Raccordement et provenance LLM — LLM-08/09

Appliquer `python -m alembic upgrade head` pour `0017_llm_run_traceability`. Les profils, snapshots et tentatives existants restent conservés. Analyse, génération, régénération et production documentaire déléguée passent par le même client ; chaque retry fournisseur est persisté avec ses paramètres effectifs. Limite métier par appel : 20 secondes, retries du profil jusqu'à trois ; tâches analyse/génération Celery : 100/110 secondes. Maintenir le heartbeat de la session pendant sa délégation (bail 120 secondes). Aucun retry automatique d'un run resté `running` après coupure ; reprise supervisée INFRA-05.

Tarification opérateur facultative : fournir `MIMIC_LLM_PRICING_JSON` à l'API et aux workers, un tableau JSON d'entrées avec `provider`, `model_name`, `date` ISO non future, `source` (120 caractères maximum), `currency` (trois lettres majuscules), `input_per_million` et `output_per_million` (nombres décimaux finis ≥ 0). Ces valeurs doivent venir d'un barème identifié par l'exploitant ; aucun tarif réel n'est préconfiguré. Le barème le plus récent applicable est figé au lancement, avec la devise, la source et la date. Catalogue invalide/absent ou usage incomplet : coût `Non disponible`. Une valeur explicitement nulle est autorisée.

Délégation technique : `POST /api/projects/{project_id}/agent-tasks/{task_id}/llm?session_id=UUID`, Bearer de session et JSON :

```json
{
  "claim_id": "UUID-claim-courant",
  "document_type": "technical_doc",
  "style": "concise",
  "context_ids": ["UUID-element-du-projet"]
}
```

`profile_version_id` facultatif : version courante active et vérifiée explicitement, sinon défaut gouverné. Plan approuvé, feuille réservée/accord courant, session active et droits `agent.task.claim`/`generation.create` du créateur exigés. Retour `202` avec ID de run/profil/statut ; rejeu identique sans redispatch, changement d'entrée refusé. Un échec passe par le retry humain de tâche puis nouveau claim/accord/délégation ; la relance documentaire générale est bloquée pour ces productions. Succès = livrable `Brouillon IA` et tâche techniquement terminée, sans validation ni fin automatique du flux.

Provenance humaine : `GET /api/projects/{project_id}/llm-runs/{analysis|generation}/{run_id}`, cookie et `project.read`, filtre projet, `no-store`. Prompts masqués, contexte figé, appels/tokens/tarif/erreurs ; liens depuis Sources et Revue. Tests : `python -m pytest tests/test_llm_run_traceability.py -q --tb=short`, PostgreSQL local dédié `mimic_test` selon le contrat de `tests/conftest.py`, aucun appel fournisseur réel. Downgrade vers `0016` destructif des traces/liens ajoutés, réservé à base jetable/restauration coordonnée. [Cas](../docs/use-cases/012-llm-run-traceability.md), [preuve](../docs/quality/proofs/llm-08-09.md).


## Messagerie et handoffs — AGENT-12

Appliquer `python -m alembic upgrade head` (`0018_agent_conversations`). Le rôle SQL de migration doit pouvoir créer le schéma `conversation` et ses FK vers `public` ; le rôle applicatif doit accéder à ces tables selon le déploiement existant. La séparation des six domaines/privilèges DB reste ouverte ; les autres tables ne sont pas déplacées. Aucun worker supplémentaire ni variable d'environnement n'est requis.

Nouvelles sessions : négocier explicitement `message.send`, `message.read`, `message.ack` ; les anciennes sessions ne gagnent pas ces capacités. Envoi avec Bearer via `POST /api/projects/{id}/agent-messages` : UUID `session_id`, `request_key`, `task_id`, résumé, contenu facultatif, priorité P0–P3, expiration ISO avec fuseau facultative (24 h par défaut, sept jours au plus). Nouveau canal : `channel_kind` = `direct` (défaut, `recipient_profile_id` requis), `broadcast` ou `thread`. Fil : `attachment` facultatif `{type,id}` vers source/version contexte/version livrable du projet ; sinon ancré sur la tâche. Réponse : `channel_id` + `reply_to_id` facultatif, sans nouvelle définition de participants. Handoff : `kind=handoff`, direct vers la compétence de `target_task_id` du même plan. `artifact_refs` contient au plus dix références typées du projet.

La source doit être une tentative propre de l'émetteur, courante avec bail vivant ou achevée, dans un flux validé en cours et un plan approuvé. Rejeu exact de `request_key` retourne le même message (`created=false`) ; autre charge avec cette clé → `409`. Résumé/contenu immuables, aucune édition/suppression ni promotion en référence.

- `GET .../agent-messages?session_id=...&channel_id=...&limit=50&offset=0` : historique technique autorisé, sans mutation de receipt.
- `POST .../agent-messages/poll`, JSON `{session_id,limit}` : réception explicite de messages non acquittés/non expirés, P0 avant P3, répétable jusqu'à ack.
- `POST .../agent-messages/{message_id}/ack`, JSON `{session_id}` : premier ack après réception ; rejeu idempotent, expiration → `409`.
- `GET .../agent-conversations` et `GET .../agent-conversations/{id}` : supervision humaine sous cookie + `agent.read`, pagination `limit/offset`, sans marquer de réception.

États individuels `pending/delivered/acknowledged/expired`, sessions et dates de livraison/ack, agrégat et handoff exposés. Une nouvelle session du même profil versionné reprend le receipt. L'ack accepte le contexte seulement ; claim/libération et validation documentaire restent leurs routes gouvernées. Les lectures projettent l'expiration effective, le polling traite les expirations par lots de 100, ainsi que celles franchies pendant la réception des messages candidats et l'ack traite celle du message ciblé ; aucun cron de purge.

Messages `Brouillon IA`, données non fiables, jamais commandes. Réponses privées, erreurs sans charge source, secrets reconnaissables refusés. Downgrade vers `0017_llm_run_traceability` détruit seulement ces conversations sur base jetable/restauration coordonnée ; conserve le schéma et les données préexistantes. [Cas détaillé](../docs/use-cases/016-agent-messaging.md), [preuve](../docs/quality/proofs/agent-12.md). Écrans et incidents livrés par AGENT-13/14 ; transport MCP natif, notifications externes et lot DB restent ouverts.

## Incidents et écrans Équipe IA — AGENT-13/14

Appliquer `python -m alembic upgrade head` pour `0019_technical_incidents`, puis relancer l'API. Même rôle SQL/prérequis `conversation` qu'AGENT-12, aucune variable ou dépendance/worker supplémentaire. La sidebar **Équipe IA** ouvre `/projects/{id}/agents` ; onglets **Mémoire**, **Coordination**, **Incidents**, filtres Projet et listes HTML sans JavaScript. Mémoire : création/correction/soumission/revue et référence active, historique et globales exactes. Coordination : canaux/fils/messages/receipts en supervision sans ack technique. Incidents : signalement, suivi, escalades, résolution et notifications personnelles.

API humaine : `/api/projects/{id}/incidents`. Cookie + `agent.read` pour consulter ; mutations avec `X-CSRF-Token`. PO/AA/CP signalent sous `incident.report`, CP gère sous `incident.manage` ; AP via guard existant. Projet archivé consultable, gestion refusée.

| Route | Contrat |
| --- | --- |
| `POST` préfixe | JSON `request_key`, `team_id`, `responsible_profile_id`, `title` (180), `description` (4000), `priority` P0–P3 (P2 par défaut), `task_id` facultatif ; UUID pour les identifiants. Retour `201`, `incident/created` ; rejeu exact sans double effet. |
| `GET` préfixe | Filtres `status`, `priority`, `profile_id`, `limit` 1–100 (défaut 50), `offset` 0–10000 ; `next_offset`. |
| `GET /{incident_id}` | Détail/historique immuable des révisions, motifs, preuves et snapshots. |
| `POST /{incident_id}/{action}` | `expected_revision` entier positif, `reason` (1000) et champs de l'action : `acknowledge`, `assign/escalate` (`responsible_profile_id`), `priority` (`priority`), `edit` (`title/description`), `resolve` (`source_refs` facultatif comme mémoire). Motif requis sauf prise en charge. |
| `GET /notifications` | Historique paginé personnel ; expiration effective sans mutation. |
| `POST /notifications/poll` | Notifications personnelles en attente/non expirées ; matérialise jusqu'à 100 expirations. |
| `POST /notifications/{id}/ack` | Ack personnel idempotent ; expiré `409`, transition d'expiration conservée. |

États `open/acknowledged/escalated/resolved`, résolution terminale. Chaque changement crée un événement et notifie profil responsable et CP actifs : 24 h, pas de fermeture automatique de l'incident, pas d'envoi externe/purge. Une révision périmée ou un rejeu différent échoue `409`. Résolution n'achève/reprend pas de tâche et ne valide aucun document. Les contenus agentiques restent `Brouillon IA`, les corrections humaines `À revoir` ; pas de statut documentaire validé pour l'incident.

API technique : `/api/projects/{id}/agent-incidents`. Négocier `incident.report/read/ack` à l'enregistrement ; anciennes sessions inchangées. `POST` préfixe : même signalement et `session_id`, Bearer/capacité `incident.report` et permission actuelle du créateur. Équipe de session et tâche de sa compétence, signalement possible durant suspension. `GET /{id}?session_id=...` et `POST /notifications/poll` avec `{session_id}` sous `incident.read` ; `POST /notifications/{id}/ack` sous `incident.ack`. Boîte isolée par projet/équipe/profil, reprise après reconnexion. Aucun endpoint technique de gestion/résolution. Un fil de `agent-messages` peut désormais recevoir `attachment={type:"technical_incident",id:"UUID"}` du même projet/équipe.

Retour arrière : downgrade `0018_agent_conversations` sur base jetable/restauration coordonnée détruit incidents/événements/notifications et leurs droits/capacités, conserve messages/tâches et remet à null les attachments vers incidents retirés. Base peuplée et régression testées sans appel LLM réel. [Cas](../docs/use-cases/016-agent-incidents.md), [ADR 018](../docs/adr/018-technical-incidents.md), [preuve](../docs/quality/proofs/agent-13-14.md). Recette humaine et gouvernance DB multi-domaines restent ouvertes.

## Dépôts, worktrees, tests et revue — AGENT-15 à 20

Appliquer `python -m alembic upgrade head` (`0020_git_development`), puis relancer API et worker. Git CLI et Docker CLI/daemon sont des prérequis opérateur pour le worker ; les tests exécutent une image **locale figée par digest** (`docker image inspect python:3.12-slim --format '{{.Id}}'` pour obtenir son ID), jamais téléchargée automatiquement. API et worker partagent les mêmes racines et catalogue. Avec un worker conteneurisé, les racines doivent être montées aux **mêmes chemins absolus** sur le daemon Docker et le worker ; le worker ne monte aucune autre ressource dans les conteneurs de tests. Prévoir des permissions de lecture UID 65534 sur le worktree de test. Les racines parentes et métadonnées Git restent administrées par l’exploitant/worker ; un client externe reçoit uniquement son espace autorisé, avec permissions OS et montages adaptés.

Variables nouvelles (aucune valeur de secret dans les exemples) :

- `MIMIC_GIT_ROOT` : répertoire absolu canonique existant contenant les dépôts locaux préprovisionnés ; pas un symlink.
- `MIMIC_WORKTREE_ROOT` : autre répertoire absolu canonique existant, hors racine des dépôts. Le serveur dérive `<projet UUID>/<worktree UUID>`.
- `MIMIC_GIT_CATALOG` : objet JSON fourni par l'exploitant, jamais par une session technique. Clé logique → chemin relatif, suites, remote et adapter de déploiement facultatifs.
- `MIMIC_GIT_ALLOW_HTTP=true` : opt-in explicite pour une forge HTTP On-Premise ; HTTPS requis par défaut. URL sans userinfo/query/fragment/redirection.
- `MIMIC_GIT_SECRET_<NOM>` : valeurs de credentials uniquement dans l'environnement sécurisé. Le catalogue conserve le **nom de variable**, pas sa valeur. Pour Gitea HTTP(S), `auth_env` référence un personal access token ; Git Smart HTTP le reçoit comme mot de passe Basic avec l'identifiant technique `x-access-token`. Le header, limité à l'URL du dépôt, passe uniquement dans l'environnement du sous-processus Git, jamais argv/config persistée/log/base ; sa forme encodée est également masquée. Le header `Authorization: token` de l'API Gitea n'est pas le protocole d'authentification Git. Un jeton `read:repository` permet la lecture ; le push exige `write:repository` et les droits du compte sur ce dépôt.

Forme du catalogue (remplacer le digest par celui de l'image locale opérateur) :

```json
{
  "backend": {
    "path": "backend.git",
    "remote": "https://forge.example.org/Maurice/backend.git",
    "auth_env": "MIMIC_GIT_SECRET_FORGE",
    "test_paths": ["tests"],
    "tests": {
      "unit": {
        "image": "sha256:0000000000000000000000000000000000000000000000000000000000000000",
        "argv": ["python", "-m", "unittest", "discover", "-s", "tests"]
      }
    }
  }
}
```

Le dépôt peut être bare (recommandé pour les merges) ou un checkout principal. Son provisionnement/clone/fetch et la création sur Gitea restent explicitement opérateur ; ce lot n'en lance pas. Git local refuse hooks/config active, filtres, alternates/grafts, symlinks et sous-modules dans les workspaces. Ne pas installer de hook exécutable ou de filtre smudge dans ce dépôt partagé. Un dépôt physique ne peut appartenir à deux projets, même via deux clés de catalogue.

Déploiement facultatif : ajouter `deployments`, objet clé destination → `{"argv":["/opt/mimic/adapters/deploy-recette"],"env_refs":{"DELIVERY_TOKEN":"MIMIC_GIT_SECRET_DELIVERY"}}`. Le programme existe et est canonique **hors racines Git/worktree** ; son empreinte est figée avec la configuration. Il reçoit `MIMIC_COMMIT`, `MIMIC_WORKTREE`, `MIMIC_OPERATION` et les seules variables explicitement référencées. Il doit vérifier sa destination, consommer le SHA exact et être idempotent ; les règles de déploiement de production et les credentials sont opérateur. Aucun argv/image/chemin/URL libre n'est reçu depuis l'API ; aucune commande shell générée.

Parcours : onglet **Dépôts et code** de **Équipe IA** (`/projects/{id}/agents/code`), rattacher AA une clé autorisée, préparer CP un worktree sur une feuille de plan approuvé, puis effectuer les phases analyse/tests rouges/implémentation/tests verts/revue/approbation AA. Le client externe écrit les fichiers et commits dans l'espace autorisé ; l'application collecte et gouverne, elle ne génère pas de code automatiquement.

API humaine sous `/api/projects/{id}/git` :

| Route | Usage |
| --- | --- |
| `GET catalog`, `GET/POST repositories`, `POST repositories/{id}/revoke` | Catalogue/rattachement/révocation ; AA pour mutation |
| `GET/POST worktrees`, `GET worktrees/{id}` | Préparation asynchrone, listes, preuves/historique |
| `POST worktrees/{id}/operations/{collect,test,merge,push,deploy,delete}` | Requête exacte avec `request_key` UUID ; `change_id` et suite/cible selon action |
| `POST operations/{id}/decision` | Accord/refus humain avec motif ; accord ≠ exécution |
| `POST operations/{id}/dispatch` | Exécution explicite ou redispatch d'un job `queued` |
| `POST operations/{id}/reconcile` | Vérifier un effet Git incertain sans le répéter |
| `POST worktrees/{id}/workflow` | Étape et révision attendue, preuve rouge/verte ; `code.approve` pour approbation finale |
| `POST reviews`, `POST reviews/{id}` | Revue exacte, commentaire fichier/hunk, correction/résolution/avis avec révision attendue |

Routes techniques sous `/api/projects/{id}/agent-git` : worktree (création/détail), opérations collect/test, workflow hors approbation, création et actions de revue. Bearer + `session_id` + capacité négociée ; create/collect/test et workflow exigent le claim courant de la feuille. Un reviewer technique consulte/commentе dans son équipe, sans accord sensible ni validation humaine. Les anciennes sessions ne reçoivent pas les nouvelles capacités automatiquement.

Jobs Celery `mimic.git.execute` par identifiant ; timeout worker 120 s, outil Git 30 s, test/deploy 40 s, sorties bornées. CPU/mémoire/réseau/fichiers sont isolés pour les tests. Toutes les suites du catalogue doivent avoir leur dernier résultat vert sur la collecte actuelle avant approbation ; résultat client déclaré inutilisable. Une collecte nouvelle exige une nouvelle revue/approbation, même si le contenu est identique. HEAD, contenu, policy, droits ou ref destination changés rendent l'accord inexécutable. Revue verte et approbation du code ne terminent pas la tâche ni ne valident un livrable.

Une pause/annulation du run bloque création, collecte, tests et intégration (merge/push/déploiement). La consultation, la revue humaine et le nettoyage approuvé restent possibles ; aucune reprise implicite.

Bornes de collecte : 200 fichiers modifiés, 2000 fichiers dans le worktree, 256 Kio par fichier, 8 Mio de contenu/historique et 100 commits. La lecture compare HEAD et contenu avant/après, refuse les espaces changeants et contrôle aussi les messages de commit. Les contenus binaires ou reconnus sensibles sont masqués et bloquent tests/approbation/intégration dans cette étape ; préparer un dépôt de code textuel compatible. La détection de secrets reste limitée aux valeurs connues et signatures reconnaissables : elle ne constitue pas un scanner exhaustif.

Actions sensibles à permissions distinctes, accord d'une heure et exécution explicite. Merge fast-forward via `update-ref` CAS, cible non checkoutée (utiliser un dépôt bare ou une branche dédiée). Push fast-forward vers la destination du catalogue, lease CAS sur l’ancienne ref, pas de création de PR/branche externe implicite. Suppression seulement propre via `git worktree remove`, jamais forcée ; branche et preuves conservées. Aucun effet depuis un message.

Reprise : broker absent → `queued` et erreur minimisée, bouton redispatch. Démarrage persisté avant effet ; redélivrance d'un `running` → `uncertain`, sans répéter l'effet. Réconciliation vérifie worktree de départ, ref Git/push ou suppression ; un déploiement incertain exige l'exploitant. Un échec create avant effet peut demander une autre préparation ; un worktree créé malgré un arrêt se réconcilie, sans écraser ses modifications. Ne jamais remettre arbitrairement une opération sensible en `queued` via SQL. Les gardes sont recontrôlées au worker, y compris retrait des droits.

Downgrade `0019_technical_incidents` : destructif **uniquement en base** pour ces neuf tables/droits/capacités, sans toucher aux fichiers/branches/dépôts. Réservé à une base jetable ou à une restauration coordonnée et arrêt des workers ; inventorier les worktrees avant rollback. [UC-016-05](../docs/use-cases/016-git-development.md), [ADR 019](../docs/adr/019-governed-git-workspaces.md).

Recette distante facultative : provisionner explicitement un dépôt **privé de test** nommé `mimic-agent15-20-recette-*`, initialisé avec `main`. Configurer `MIMIC_TEST_GITEA_REMOTE`, `MIMIC_TEST_GITEA_WRITES=true`, `MIMIC_GIT_SECRET_FORGE`, `MIMIC_TEST_IMAGE` et la base PostgreSQL locale de test, puis lancer `python -m pytest -q tests/test_git_gitea.py --tb=short`. Ce test crée des branches de recette uniques et les conserve comme preuves ; il vérifie que `main` reste identique. Aucun dépôt de production ne doit être utilisé. Le test du jeton de lecture seule exige en plus `MIMIC_TEST_GITEA_READ_ONLY=true` et se lance séparément avec ce credential ; ne pas activer ce mode avec le jeton d'écriture. Les cas distants restent ignorés sans cet opt-in et ce credential. Une redirection après renommage exige une vérification opérateur de l'identité puis une mise à jour explicite du catalogue ; aucun suivi automatique.

## Runtimes et quotas — AGENT-21/22

Après `python -m alembic upgrade head` (migration additive `0021_runtime_quotas`), ouvrir Projet → Équipe IA → Runtimes et quotas. L'Architecte configure un runtime projet et ses rattachements tâche/session/compétence/projet ; le Chef de Projet définit les quotas tokens, secondes, concurrence et coût+devise. Version attendue et motif obligatoires pour révision ; retirer une limite conserve les consommations. Les profils LLM doivent être vérifiés dans l'administration. Aucune variable nouvelle ni nouveau service ; tarif opérateur `MIMIC_LLM_PRICING_JSON` de LLM-09.

API humaine, cookie + `X-CSRF-Token` : `GET /api/projects/{id}/runtimes`, `POST .../versions`, `POST .../bindings`, `POST .../quotas`, `GET .../usage?scope=project|agent|task|session|model&scope_id=...&offset=...`, `POST .../usage/{usage_id}/reconcile` avec motif et `stopped_confirmed=true`. Entrées détaillées dans OpenAPI. Agent = UUID `agent_team_skill` ; modèle = SHA-256 de la paire JSON `[provider,model_name]`, exposé parmi les filtres IHM. La consommation est cumulative depuis cette migration ; les traces antérieures ne sont pas reconstruites.

Client connecté : nouvelle session négociant `task.runtime`, claim approuvé puis `POST /api/projects/{id}/agent-tasks/{task_id}/runtime?session_id=...` (Bearer, `claim_id`). Le client doit correspondre au runtime rattaché. Rejeu retourne la même admission ; progression utilise l'API existante et termine l'admission. Le backend ne lance pas le processus client. Ses tokens/coût sont `Non disponible`, ses quotas tokens/coût ou modèle inconnu bloquent l'admission. Durée et concurrence sont mesurées du côté serveur, sans contrôle physique du client.

Worker interne : délégation `.llm` historique, routage automatique figé, option mémoire de référence bornée ; analyse/génération/régénération ordinaires comptent aussi au niveau projet/modèle. Une réservation d'entrée utilise les octets des messages + marge de 1024 ; sortie bornée par le profil, durée de chaque appel ≤ 20 s et deadline documentaire ≤ 80 s (attente de queue incluse) ; client ≤ 3600 s avec heartbeats et leases valides. Chaque retry réserve. Les charges financières sont arrondies au micro supérieur (estimation affichée LLM-09 au plus proche). Une métrique manquante conserve la charge prudente et affiche `Non disponible` ; un dépassement fournisseur réel est conservé et bloque les admissions suivantes. Ce contrôle préventif n'est pas une garantie de facture fournisseur.

Appel interrompu : constater l'arrêt réel du worker/client, puis clôturer comme incertain dans la page ou l'API, avec motif. Le créneau est libéré mais les charges conservées ; le run et la tâche sont clôturés en échec, sans rejeu automatique ni utilisation d'un retour tardif. L'expiration du claim seule ne débloque pas un appel actif. Relance de tâche ensuite par le retry humain du coordinateur existant, visant la tentative concernée. Budgets périodiques, seuils/alertes et suspensions des nouvelles admissions sont désormais livrés par AGENT-23 ci-dessous ; ordonnanceur AGENT-31 ouvert.

Retour arrière de 0021 : réservé à une base jetable/restauration coordonnée, détruit les cinq tables de gouvernance et les droits nouveaux ; tâches, sessions, traces LLM et livrables conservés. [Cas et limites](../docs/use-cases/016-agent-runtimes.md), [ADR](../docs/adr/020-runtime-quotas.md).

## Pilotage projet — AGENT-25

Ouvrir **Pilotage** depuis le projet ou **Pilotage projet** depuis l’espace Équipe IA. La page `/projects/{id}/pilotage` et `GET /api/projects/{id}/pilotage` partagent une projection de lecture sous `project.read`/`pilotage.read` ; `agent.read` contrôle les activités, compteurs et consommation agentiques. Sans ce droit, ces données sont masquées et indisponibles. Les projets archivés restent consultables.

Filtres `kind` (documents/sessions/tasks/messages/worktrees/reviews/quotas/alerts), `state` et `offset` 0–10 000, pages de 20. Compteurs globaux avant filtre ; total et activités après filtres ; ordre date/type/ID décroissant. Le filtre projet humain ne propose que les projets lisibles et autorisés au pilotage, puis remet les filtres à zéro lors du changement. Les agents actifs comptent les couples équipe/compétence, les sessions séparément ; messages distincts et livraisons par profil ont des compteurs distincts. Sessions/messages expirés sont évalués à la lecture sans mutation persistante.

La liste montre les blocages des feuilles, les worktrees conservés, les revues non terminées sur la dernière collecte, les incidents non résolus, opérations Git incertaines et appels runtime à examiner. Les refus de quota/tarif avant appel fournisseur restent visibles même sans ligne d’usage. Les textes libres des messages, prompts, manifestes, logs et credentials restent hors projection ; titres masqués/échappés. Aucun GET ne demande un accord, acquitte un message, récupère une tâche ou exécute Git. Les décisions passent par leurs écrans/API propriétaires.

Quotas cumulatifs AGENT-22 et consommation du registre sont séparés des budgets périodiques/alertes budgétaires AGENT-23, désormais raccordés dans le complément ci-dessous. Les mesures manquantes ou devises multiples ne produisent pas de total financier implicite. Aucun nouveau prérequis, worker ou credential de forge ; la migration 0022 ajoute la gouvernance budgétaire. Tests : `python -m pytest -q tests/test_pilotage.py --tb=short` sur PostgreSQL local `mimic_test` selon le contrat existant. [Cas et limites](../docs/use-cases/016-agent-pilotage.md), [preuve](../docs/quality/proofs/agent-25.md).

## Budgets périodiques AGENT-23 et Pilotage AGENT-25

Appliquer `python -m alembic upgrade head` : migration additive `0022_execution_budgets`, trois tables et permission CP `budget.configure`. Aucune nouvelle variable, dépendance ou infrastructure. Retour arrière sur base jetable : `python -m alembic downgrade 0021_runtime_quotas` détruit uniquement budgets/alertes/suspensions et ce droit ; sauvegarde/restauration coordonnée exigée en production.

Équipe IA → Runtimes et quotas, sections Budgets et Alertes ; Pilotage rend les compteurs et filtres correspondants. API humaine cookie/CSRF : `GET /api/projects/{id}/budgets`, `POST .../budgets/versions`, `POST .../budgets/evaluate`, `POST .../budgets/alerts/{id}/acknowledge`, `POST .../budgets/suspensions/{id}/resume`. Configuration/acquittement CP ; reprise avec agent.run.manage ; lecture agent.read. POST versions exige scope/scope_id, expected_version, period daily/weekly/monthly, metric tokens/seconds/cost, amount, warning_percent, devise pour coût seulement, enabled et reason. Reprise/acquittement exigent reason.

Périodes UTC ; réservations et charges imputées au début de l'appel. Révision ne remet pas la consommation à zéro. Les IDs des politiques de runtime_usage incluent les versions exactes de quotas et budgets appliqués. Une suspension bloque les nouvelles admissions, conserve les appels déjà admis et exige une reprise distincte après résolution du blocage ; acquittement sans reprise implicite. [Cas et limites](../docs/use-cases/016-agent-budgets.md), [preuve](../docs/quality/proofs/agent-23-25.md).

Tests : `python -m pytest -q tests/test_agent_budgets.py tests/test_pilotage.py tests/test_runtime_quotas.py --tb=short`, avec MIMIC_TEST_DATABASE_URL vers une base PostgreSQL locale dédiée mimic_test.

## Classement organisationnel et isolation — AGENT-27

Appliquer `python -m alembic upgrade head` : `0023_organization_scope` crée `organization` et `project.organization_id` facultatif. Aucun projet existant n'est déplacé, aucun droit hérité ou partage global n'est créé. Ce lot ne fournit pas d'API ou d'écran de gestion d'organisation : le rattachement demeure un socle de données pour un futur flux administré. Les contrôles API continuent à utiliser l'ACL du projet exact et l'équipe de la session ; les conventions globales exigent un partage explicite par projet/version. Aucun service ni variable supplémentaire. Retour arrière `python -m alembic downgrade 0022_execution_budgets` sur base jetable ou restauration coordonnée : la classification organisationnelle est perdue, les objets projet restent.

Tests : `python -m pytest -q tests/test_agent_isolation.py tests/test_agent_team_routing.py tests/test_project_memory.py --tb=short` avec `MIMIC_TEST_DATABASE_URL` vers la base locale dédiée `mimic_test`. [Cas](../docs/use-cases/016-agent-isolation.md), [ADR](../docs/adr/022-organization-scope-isolation.md), [preuve](../docs/quality/proofs/agent-27.md).
