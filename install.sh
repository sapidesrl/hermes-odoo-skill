#!/usr/bin/env bash
# Installs the Odoo skill into Hermes Agent. Non-destructive: an existing
# copy is moved to a timestamped backup, never deleted.
set -euo pipefail

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/skills/odoo"
HERMES_DIR="${HERMES_HOME:-$HOME/.hermes}"
DEST="$HERMES_DIR/skills/productivity/odoo"

if [ ! -f "$SRC/SKILL.md" ]; then
  echo "Cannot find $SRC/SKILL.md" >&2
  exit 1
fi

mkdir -p "$(dirname "$DEST")"
if [ -e "$DEST" ]; then
  # Kept outside the skills folder so Hermes does not load it as a duplicate.
  BACKUP="$HERMES_DIR/skill-backups/odoo-$(date +%Y%m%d-%H%M%S)"
  mkdir -p "$(dirname "$BACKUP")"
  mv "$DEST" "$BACKUP"
  echo "Existing skill moved to $BACKUP"
fi
cp -R "$SRC" "$DEST"
chmod +x "$DEST/scripts/odoo_cli.py"
echo "Installed Odoo skill in $DEST"

ENV_FILE="$HERMES_DIR/.env"
if ! grep -q '^ODOO_URL=' "$ENV_FILE" 2>/dev/null; then
  cat <<EOF

Next step - add your Odoo credentials to $ENV_FILE :

  ODOO_URL=https://mycompany.odoo.com
  ODOO_USERNAME=hermes@mycompany.com
  ODOO_API_KEY=xxxxxxxxxxxxxxxx
  # ODOO_DB=mycompany            # only if the server hosts several databases
  # ODOO_HERMES_ALLOW_WRITES=1   # enable (previewed, journaled, undoable) writes

Then test:  python3 "$DEST/scripts/odoo_cli.py" doctor
EOF
fi
