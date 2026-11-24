# hermes-odoo-skill

Un skill pour [Hermes Agent](https://github.com/NousResearch/hermes-agent) qui connecte Hermes à
**Odoo** (v14 à v19+, Community, Enterprise, Odoo Online / Odoo.sh) et lui permet de le gérer
**sans jamais rien détruire**.

> « Hermes, fais-moi le point du matin. »
> « Prépare-moi l'appel avec Acme : ce qu'ils ont acheté, ce qu'ils doivent, les opportunités en cours. »
> « Relance les factures de plus de 30 jours : rédige les mails et planifie une activité sur chacune. »

## Ce que sait faire Hermes avec ce skill

| | |
|---|---|
| 🔎 **Explorer** | chercher n'importe quel enregistrement avec un mini-langage lisible (`name ~ acme, amount_total > 1000, date_order >= @month_start`), lire une fiche complète, compter, regrouper/agréger, recherche globale (`lookup`), historique du chatter |
| 📈 **Pulse** | le « pouls » de l'entreprise en une commande : ventes du mois, devis ouverts, factures en retard, pipeline CRM, livraisons en retard, tâches en retard, tickets ouverts |
| 👤 **Client 360** | tout sur un client sur un écran : coordonnées, dernières commandes, impayés, opportunités, tickets, activités, derniers messages |
| ✅ **To-do** | mes activités en retard / du jour / à venir |
| 🧹 **Hygiène** | audit (lecture seule) de la qualité des contacts : emails/téléphones manquants, doublons, emails mal formés |
| ✍️ **Agir** | créer, modifier, archiver/désarchiver, poster une note interne, planifier une activité, lancer une action métier autorisée |
| ⏪ **Annuler** | chaque modification est journalisée avec les valeurs d'avant ; `undo` les restaure |
| 📤 **Exporter** | export CSV local (n'écrase jamais un fichier) |

## Non destructif, par conception

Les garde-fous sont **dans le code**, pas seulement dans les consignes données à l'IA :

1. **Lecture seule par défaut** – les écritures exigent `ODOO_HERMES_ALLOW_WRITES=1`.
2. **Simulation (dry-run) par défaut** – toute écriture affiche d'abord un diff champ par champ ;
   rien n'est écrit sans `--apply`, et Hermes doit obtenir votre accord explicite.
3. **Aucune suppression, jamais** – `unlink` et les méthodes destructrices (`cancel`, `reset`,
   `merge`, `uninstall`, …) sont bloquées. « Supprimer » devient « archiver » (réversible).
4. **Journal + undo** – `~/.hermes/odoo/journal.jsonl` garde l'avant/après ; `undo` restaure et
   refuse si quelqu'un a modifié l'enregistrement entre-temps.
5. **Modèles et champs protégés** – utilisateurs, groupes, sociétés, `ir.*`, plan comptable, taxes,
   mots de passe, clés API, `state`, `active`…
6. **Plafond anti-modification de masse** – 25 enregistrements par opération par défaut.
7. **Relations sûres** – les many2many/one2many ne font qu'*ajouter* ; vider/supprimer est refusé.
8. **Actions métier sur liste blanche** – `action_confirm` & co. seulement si vous les autorisez.

Détails : [`skills/odoo/references/safety.md`](skills/odoo/references/safety.md).

## Installation

```bash
git clone https://github.com/sapidesrl/hermes-odoo-skill.git
cd hermes-odoo-skill
./install.sh          # copie dans ~/.hermes/skills/productivity/odoo (sauvegarde l'existant)
```

Puis ajoutez dans `~/.hermes/.env` :

```dotenv
ODOO_URL=https://monentreprise.odoo.com
ODOO_USERNAME=hermes@monentreprise.com
ODOO_API_KEY=xxxxxxxxxxxxxxxx         # Odoo > Mon profil > Sécurité du compte > Nouvelle clé API
# ODOO_DB=monentreprise               # seulement si plusieurs bases (Odoo Online : auto-détecté)
# ODOO_HERMES_ALLOW_WRITES=1          # activer les écritures (prévisualisées, journalisées, annulables)
# ODOO_HERMES_ALLOWED_METHODS=action_confirm
```

Conseil : créez un **utilisateur Odoo dédié** (« Hermes ») avec des droits limités — c'est la
meilleure protection, et toutes ses actions sont tracées dans Odoo.

Vérification :

```bash
python3 ~/.hermes/skills/productivity/odoo/scripts/odoo_cli.py doctor
```

Ensuite, parlez simplement d'Odoo à Hermes ; le skill se charge tout seul (ou via `/odoo`).

## Structure

```
skills/odoo/
├── SKILL.md                 # instructions pour Hermes (règles d'or, commandes, playbooks)
├── scripts/odoo_cli.py      # le pont Odoo : Python 3.8+, aucune dépendance
└── references/
    ├── models.md            # modèles et champs Odoo courants
    ├── recipes.md           # requêtes prêtes à l'emploi
    └── safety.md            # le modèle de sécurité complet
tests/                       # faux serveur Odoo (XML-RPC + JSON-2) et tests
```

## Odoo.com (Odoo Online) – formule Enterprise

Le skill passe par l'**API externe officielle** d'Odoo, rien n'est à installer dans Odoo
(Odoo Online n'accepte de toute façon pas les modules tiers).

- ⚠️ Odoo ne donne accès à l'API externe que sur la formule **Personnalisé (Custom)**. Elle est
  indisponible sur **Standard** et sur « Une application gratuite ». Vérifiez votre formule dans
  votre abonnement odoo.com.
- La base est détectée automatiquement depuis le sous-domaine (`monentreprise.odoo.com`).
- Les versions Online (`saas~19.x`) utilisent automatiquement l'API JSON-2 avec la clé API.
- Les clés API expirent au plus tard après **3 mois** : pensez à la renouveler (Mon profil >
  Sécurité du compte) et à mettre à jour `ODOO_API_KEY`.

## Compatibilité

- XML-RPC (Odoo 14 → 19) avec mot de passe ou clé API.
- API **JSON-2** (Odoo 19+) automatiquement utilisée avec une clé API — XML-RPC est déprécié
  à partir d'Odoo 20. Forçable via `ODOO_PROTOCOL=xmlrpc|json2`.

## Tests

```bash
python3 -m unittest discover -s tests -v
```

Les tests tournent contre un faux serveur Odoo en mémoire (les deux protocoles) et vérifient
notamment que `unlink` n'est jamais appelé, que le dry-run n'écrit rien et que `undo` restaure.

## Licence

MIT
